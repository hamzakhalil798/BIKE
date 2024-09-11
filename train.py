import os
import sys
import time
import argparse

import torch
import torch.nn as nn
from torch.nn import functional as F
from torch.utils.data import DataLoader
from torch.nn.parallel import DistributedDataParallel
import torch.distributed as dist
import torch.backends.cudnn as cudnn
from torch.cuda.amp import GradScaler
import torchvision
import numpy as np

from utils.utils import init_distributed_mode, epoch_saving, best_saving, AverageMeter, reduce_tensor, accuracy, create_logits, gen_label, gather_labels
from utils.logger import setup_logger
import clip

from pathlib import Path
import yaml
import pprint
from dotmap import DotMap

import datetime
import shutil
from contextlib import suppress

from modules.video_clip import video_header
from utils.NCELoss import NCELoss, DualLoss
from utils.Augmentation import get_augmentation
from utils.solver import _optimizer, _lr_scheduler
from modules.text_prompt import text_prompt

class AllGather(torch.autograd.Function):
    """An autograd function that performs allgather on a tensor."""

    @staticmethod
    def forward(ctx, tensor):
        # When not using distributed setup, just pass the tensor as is
        output = [tensor]
        ctx.rank = 0  # Since no distribution, we can set rank to 0
        ctx.batch_size = tensor.shape[0]
        return torch.cat(output, dim=0)  # Output is just the input tensor itself

    @staticmethod
    def backward(ctx, grad_output):
        # Return the gradient of the output, slice for the rank (which is 0)
        return grad_output[ctx.batch_size * ctx.rank : ctx.batch_size * (ctx.rank + 1)], None


allgather = AllGather.apply

def update_dict(dict):
    new_dict = {}
    for k, v in dict.items():
        new_dict[k.replace('module.', '')] = v
    return new_dict

def get_parser():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', '-cfg', type=str, default='clip.yaml', help='global config file')
    parser.add_argument('--log_time', default='001')
    parser.add_argument('--dist_url', default='env://',
                        help='url used to set up distributed training')
    parser.add_argument('--world_size', default=1, type=int,
                        help='number of distributed processes')                        
    parser.add_argument("--local_rank", type=int,
                        help='local rank for DistributedDataParallel')
    parser.add_argument(
        "--precision",
        choices=["amp", "fp16", "fp32"],
        default="amp",
        help="Floating point precition."
    )                        
    args = parser.parse_args()
    return args



def main(args):
    global best_prec1
    """ Training Program """
    init_distributed_mode(args)
    if args.distributed:
        print('[INFO] turn on distributed train', flush=True)
    else:
        print('[INFO] turn off distributed train', flush=True)

    with open(args.config, 'r') as f:
        config = yaml.load(f, Loader=yaml.FullLoader)

    working_dir = os.path.join(config['data']['output_path'], config['data']['dataset'], config['network']['arch'] , args.log_time)


    # if dist.get_rank() == 0:
    Path(working_dir).mkdir(parents=True, exist_ok=True)
    shutil.copy(args.config, working_dir)
    shutil.copy('train.py', working_dir)


    # build logger, print env and config
    logger = setup_logger(output=working_dir,
                          distributed_rank=0,
                          name=f'BIKE')
    logger.info("------------------------------------")
    logger.info("Environment Versions:")
    logger.info("- Python: {}".format(sys.version))
    logger.info("- PyTorch: {}".format(torch.__version__))
    logger.info("- TorchVison: {}".format(torchvision.__version__))
    logger.info("------------------------------------")
    pp = pprint.PrettyPrinter(indent=4)
    logger.info(pp.pformat(config))
    logger.info("------------------------------------")
    logger.info("storing name: {}".format(working_dir))



    config = DotMap(config)

    device = "cpu"
    if torch.cuda.is_available():
        device = "cuda"
        cudnn.benchmark = True

    # fix the seed for reproducibility
    seed = config.seed + 0
    torch.manual_seed(seed)
    np.random.seed(seed)


    # get fp16 model and weight
    model, clip_state_dict = clip.load(
        config.network.arch,
        device=device,jit=False,
        internal_modeling=config.network.tm,
        T=config.data.num_segments,
        dropout=config.network.drop_out,
        emb_dropout=config.network.emb_dropout,
        pretrain=config.network.init,
        joint_st = config.network.joint_st) # Must set jit=False for training  ViT-B/32

    transform_train = get_augmentation(True, config)
    transform_val = get_augmentation(False, config)


    logger.info('train transforms: {}'.format(transform_train.transforms))
    logger.info('val transforms: {}'.format(transform_val.transforms))


    video_head = video_header(
        config.network.sim_header,
        config.network.interaction,
        clip_state_dict)


    video_head = video_head.to(device)  # Move video_head to GPU

    if args.precision == "amp" or args.precision == "fp32":
        model = model.float()
        video_head=video_head.float()

    if config.data.dataset == 'charades':
        from datasets.charades import Video_dataset
        train_data = Video_dataset(
            config.data.train_root, config.data.train_list,
            config.data.label_list, num_segments=config.data.num_segments,
            modality=config.data.modality,
            image_tmpl=config.data.image_tmpl, random_shift=config.data.random_shift,
            transform=transform_train, dense_sample=config.data.dense,
            fps=config.data.fps)
        val_data = Video_dataset(
            config.data.val_root, config.data.val_list, config.data.label_list,
            random_shift=False, num_segments=config.data.num_segments,
            modality=config.data.modality,
            image_tmpl=config.data.image_tmpl,
            transform=transform_val, test_mode=True, dense_sample=config.data.dense)            
    else:
        from datasets.video import Video_dataset
        train_data = Video_dataset(
            config.data.train_root, config.data.train_list,
            config.data.label_list, num_segments=config.data.num_segments,
            modality=config.data.modality,
            image_tmpl=config.data.image_tmpl, random_shift=config.data.random_shift,
            transform=transform_train, dense_sample=config.data.dense)
        val_data = Video_dataset(
            config.data.val_root, config.data.val_list, config.data.label_list,
            random_shift=False, num_segments=config.data.num_segments,
            modality=config.data.modality,
            image_tmpl=config.data.image_tmpl,
            transform=transform_val, dense_sample=config.data.dense)            

    ################ Few shot data for training ###########
    if config.data.shot:
        cls_dict = {}
        for item  in train_data.video_list:
            if item.label not in cls_dict:
                cls_dict[item.label] = [item]
            else:
                cls_dict[item.label].append(item)
        import random
        select_vids = []
        K = config.data.shot
        for category, v in cls_dict.items():
            slice = random.sample(v, K)
            select_vids.extend(slice)
        n_repeat = len(train_data.video_list) // len(select_vids)
        train_data.video_list = select_vids * n_repeat
        # print('########### number of videos: {} #########'.format(len(select_vids)))
    ########################################################


    # train_sampler = torch.utils.data.distributed.DistributedSampler(train_data)                       
    train_loader = DataLoader(train_data,
        batch_size=config.data.batch_size, num_workers=config.data.workers,
        sampler=None, drop_last=True)

    # val_sampler = torch.utils.data.distributed.DistributedSampler(val_data, shuffle=False)
    val_loader = DataLoader(val_data,
        batch_size=config.data.batch_size,num_workers=config.data.workers,
        sampler=None, drop_last=False)

    loss_type = config.solver.loss_type
    if loss_type == 'NCE':
        criterion = NCELoss()
    elif loss_type == 'DS':
        criterion = DualLoss()
    else:
        raise NotImplementedError

    start_epoch = config.solver.start_epoch
    
    if config.pretrain:
        if os.path.isfile(config.pretrain):
            logger.info("=> loading checkpoint '{}'".format(config.pretrain))
            checkpoint = torch.load(config.pretrain, map_location=device)
            model.load_state_dict(checkpoint['model_state_dict'])
            video_head.load_state_dict(checkpoint['fusion_model_state_dict'])
            del checkpoint
        else:
            logger.info("=> no checkpoint found at '{}'".format(config.resume))
    
    if config.resume:
        if os.path.isfile(config.resume):
            logger.info("=> loading checkpoint '{}'".format(config.resume))
            checkpoint = torch.load(config.resume, map_location=device)
            model.load_state_dict(update_dict(checkpoint['model_state_dict']))
            video_head.load_state_dict(update_dict(checkpoint['fusion_model_state_dict']))
            start_epoch = checkpoint['epoch'] + 1
            logger.info("=> loaded checkpoint '{}' (epoch {})"
                   .format(config.evaluate, checkpoint['epoch']))
            del checkpoint
        else:
            logger.info("=> no checkpoint found at '{}'".format(config.pretrain))

    classes = text_prompt(train_data)
    n_class = classes.size(0)

    if config.network.fix_text:
        for name, param in model.named_parameters():
            if "visual" not in name and "logit_scale" not in name:
                param.requires_grad_(False)
  
    if config.network.fix_video:
        for name, param in model.named_parameters():
            if "visual" in name:
                param.requires_grad_(False)

    optimizer = _optimizer(config, model, video_head)
    lr_scheduler = _lr_scheduler(config, optimizer)

    if args.distributed:
        model = DistributedDataParallel(model.cuda(), device_ids=[args.gpu])
        if config.network.sim_header == "None" and config.network.interaction in ['DP', 'VCS']:
            video_head_nomodule = video_head
        else:
            video_head = DistributedDataParallel(video_head.cuda(), device_ids=[args.gpu])
            video_head_nomodule = video_head.module
    video_head_nomodule = video_head
        

    scaler = GradScaler() if args.precision == "amp" else None

    best_prec1 = 0.0
    if config.solver.evaluate:
        logger.info(("===========evaluate==========="))

        if config.data.dataset == 'charades':
            prec1, output_list, labels_list = validate_mAP(
                start_epoch,
                val_loader, classes, device,
                model, video_head, config, n_class, logger)
        else:
            prec1, output_list, labels_list = validate(
                start_epoch,
                val_loader, classes, device,
                model, video_head, config, n_class, logger)
        return

    #############
    save_score = True if config.data.select_topk_attributes else False
    #############

    for epoch in range(start_epoch, config.solver.epochs):
        if args.distributed:
            train_loader.sampler.set_epoch(epoch)        

        train(model, video_head, train_loader, optimizer, criterion, scaler,
              epoch, device, lr_scheduler, config, classes, logger)

        if (epoch+1) % config.logging.eval_freq == 0:
            if config.data.dataset == 'charades':
                prec1, output_list, labels_list = validate_mAP(epoch, val_loader, classes, device, model, video_head, config, n_class, logger)
            else:
                prec1, output_list, labels_list = validate(epoch, val_loader, classes, device, model, video_head, config, n_class, logger, save_score)

            # if dist.get_rank() == 0:
            is_best = prec1 > best_prec1
            best_prec1 = max(prec1, best_prec1)
            logger.info('Testing: {}/{}'.format(prec1,best_prec1))
            logger.info('Saving:')
            filename = "{}/last_model.pt".format(working_dir)

            epoch_saving(epoch, model, video_head_nomodule, optimizer, filename)
            if is_best:
                best_saving(working_dir, epoch, model, video_head_nomodule, optimizer)
                if save_score:
                    save_sims(output_list, labels_list)



def train(model, video_head, train_loader, optimizer, criterion, scaler,
          epoch, device, lr_scheduler, config, classes, logger):
    """ train a epoch """
    batch_time = AverageMeter()
    data_time = AverageMeter()
    losses = AverageMeter()
    img_losses = AverageMeter()
    text_losses = AverageMeter()

    model.train()
    video_head.train()
    autocast = torch.cuda.amp.autocast if args.precision == 'amp' else suppress
    end = time.time()

    for i,(images, list_id) in enumerate(train_loader):

        if config.solver.type != 'monitor':
            if (i + 1) == 1 or (i + 1) % 10 == 0:
                lr_scheduler.step(epoch + i / len(train_loader))
        # lr_scheduler.step()
        images = images.to(device)
        
        data_time.update(time.time() - end)
        # b t3 h w
        images = images.view((-1,config.data.num_segments,3)+images.size()[-2:])  # bt 3 h w
        b,t,c,h,w = images.size()

        images= images.view(-1,c,h,w) # omit the Image.fromarray if the images already in PIL format, change this line to images=list_image if using preprocess inside the dataset class

        texts = classes # n_cls 77
        texts = texts.to(device)  # Move texts to the same device

        with autocast():
            if config.solver.loss_type in ['NCE', 'DS']:
                texts = texts[list_id]  # bs 77
                image_embedding, cls_embedding, text_embedding, logit_scale = model(images, texts, return_token=True)
                image_embedding = image_embedding.view(b,t,-1)
                # gather
                image_embedding = allgather(image_embedding)
                if text_embedding is not None:
                    text_embedding = allgather(text_embedding)
                cls_embedding = allgather(cls_embedding)     

                logits = logit_scale * video_head(image_embedding, text_embedding, cls_embedding)
                
                list_id = gather_labels(list_id.to(device))  # bs -> n_gpu * bs

                ground_truth = torch.tensor(gen_label(list_id),dtype=image_embedding.dtype,device=device)
                # gt = [bs bs]
                loss_imgs = criterion(logits, ground_truth)
                loss_texts = criterion(logits.T, ground_truth)
                loss = (loss_imgs + loss_texts)/2
            else:
                raise NotImplementedError

            # loss regularization
            loss = loss / config.solver.grad_accumulation_steps

        if scaler is not None:
            # back propagation
            scaler.scale(loss).backward()
            if (i + 1) % config.solver.grad_accumulation_steps == 0:
                scaler.step(optimizer)
                scaler.update()
                optimizer.zero_grad()  # reset gradient
        else:
            # back propagation
            loss.backward()
            if (i + 1) % config.solver.grad_accumulation_steps == 0:
                optimizer.step()  # update param
                optimizer.zero_grad()  # reset gradient

        losses.update(loss.item(), logits.size(0))



        batch_time.update(time.time() - end)
        end = time.time()
        cur_iter = epoch * len(train_loader) + i
        max_iter = config.solver.epochs * len(train_loader)
        eta_sec = batch_time.avg * (max_iter - cur_iter + 1)
        eta_sec = str(datetime.timedelta(seconds=int(eta_sec)))

        if i % config.logging.print_freq == 0:
            logger.info(('Epoch: [{0}][{1}/{2}], lr: {lr:.2e}, eta: {3}\t'
                         'Time {batch_time.val:.3f} ({batch_time.avg:.3f})\t'
                         'Data {data_time.val:.3f} ({data_time.avg:.3f})\t'
                         'Loss {loss.val:.4f} ({loss.avg:.4f})'.format(
                             epoch, i, len(train_loader), eta_sec, batch_time=batch_time, data_time=data_time, loss=losses,
                             lr=optimizer.param_groups[-1]['lr'])))




def validate(epoch, val_loader, classes, device, model, video_head, config, n_class, logger, return_sim=False):
    top1 = AverageMeter()
    top5 = AverageMeter()
    sims_list = []
    labels_list = []
    model.eval()
    video_head.eval()

    with torch.no_grad():
        text_inputs = classes.to(device)  # [n_cls, 77]
        cls_feature, text_features = model.encode_text(text_inputs, return_token=True)  # [n_cls, feat_dim]
        for i, (image, class_id) in enumerate(val_loader):
            image = image.view((-1, config.data.num_segments, 3) + image.size()[-2:])
            b, t, c, h, w = image.size()
            class_id = class_id.to(device)
            image_input = image.to(device).view(-1, c, h, w)
            image_features = model.encode_image(image_input).view(b, t, -1)
            similarity = video_head(image_features, text_features, cls_feature)

            similarity = similarity.view(b, -1, n_class).softmax(dim=-1)  # [bs, n_frames, n_cls]
            similarity = similarity.mean(dim=1, keepdim=False)  # [bs, n_cls]

            if return_sim:
                sims = allgather(similarity)
                labels = gather_labels(class_id)
                sims_list.append(sims)
                labels_list.append(labels)

            prec = accuracy(similarity, class_id, topk=(1, 5))
            prec1 = reduce_tensor(prec[0])
            prec5 = reduce_tensor(prec[1])

            top1.update(prec1.item(), class_id.size(0))
            top5.update(prec5.item(), class_id.size(0))

            if i % config.logging.print_freq == 0:
                logger.info(
                    ('Test: [{0}/{1}]\t'
                     'Prec@1 {top1.val:.3f} ({top1.avg:.3f})\t'
                     'Prec@5 {top5.val:.3f} ({top5.avg:.3f})'.format(
                         i, len(val_loader), top1=top1, top5=top5)))
    logger.info(('Testing Results: Prec@1 {top1.avg:.3f} Prec@5 {top5.avg:.3f}'
        .format(top1=top1, top5=top5)))
    if return_sim:
        return top1.avg, sims_list, labels_list
    else:
        return top1.avg, None, None

def validate_mAP(epoch, val_loader, classes, device, model, video_head, config, n_class, logger):
    mAP = AverageMeter()
    model.eval()
    video_head.eval()
    from torchnet import meter
    maper = meter.mAPMeter()
    sims_list = []
    labels_list = []

    with torch.no_grad():
        text_inputs = classes.to(device)  # [400, 77]
        cls_feature, text_features = model.encode_text(text_inputs, return_token=True)  # [400, 512]
        for i, (image, class_id) in enumerate(val_loader):
            image = image.view((-1, config.data.num_segments, 3) + image.size()[-2:])
            b, t, c, h, w = image.size()
            class_id = class_id.to(device)
            image_input = image.to(device).view(-1, c, h, w)
            image_features = model.encode_image(image_input).view(b, t, -1)
            similarity = video_head(image_features, text_features, cls_feature)

            similarity = similarity.view(b, -1, n_class).softmax(dim=-1)  # [bs, 16, 400]
            similarity = similarity.mean(dim=1, keepdim=False)  # [bs, 400]
            similarity = F.softmax(similarity, dim=1)
            output = allgather(similarity)
            labels = gather_labels(class_id)
            sims_list.append(output)
            labels_list.append(labels)

            maper.add(output, labels)
            mAP.update(maper.value().numpy(),labels.size(0))

            if i % config.logging.print_freq == 0:
                logger.info(
                    ('Test: [{0}/{1},mAP:{map:.3f}]\t'.format(i, len(val_loader), map=mAP.avg * 100)))

    logger.info(('Testing Results mAP === {mAP_result:.3f}'.format(mAP_result=mAP.avg * 100)))
    return mAP.avg * 100, sims_list, labels_list


def save_sims(output_list, labels_list):
    outputs_sim = torch.cat(output_list, dim=0)
    labels_list_res = torch.cat(labels_list, dim=0)
    prec = accuracy(outputs_sim, labels_list_res, topk=(1, 5))
    torch.save(outputs_sim, 'video_sentence_fusion/k400_video_sims.pt')
    torch.save(labels_list_res, 'video_sentence_fusion/k400_video_labels.pt')
    # print('outputs_sim.shape==', outputs_sim.shape)
    # print('labels_list_res.shape===', labels_list_res.shape)
    # print('top1====', prec[0].item())

if __name__ == '__main__':
    args = get_parser() 
    main(args)

