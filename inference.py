import os
import argparse

import torch
import torch.nn as nn
from torch.utils.data import DataLoader
from torch.nn.parallel import DistributedDataParallel
import torch.distributed as dist
import torch.backends.cudnn as cudnn
import torchvision
import torch.nn.functional as F
import time
from utils.utils import init_distributed_mode, AverageMeter, reduce_tensor, accuracy
import clip

import yaml
from dotmap import DotMap
from datasets.video import Video_dataset
from datasets.transforms import GroupScale, GroupCenterCrop, Stack, ToTorchFormatTensor, GroupNormalize, GroupOverSample, GroupFullResSample
from modules.video_clip import video_header
from modules.text_prompt import text_prompt


def get_parser():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=str, help='global config file')
    parser.add_argument('--weights', type=str, default=None)
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
    parser.add_argument('--test_crops', type=int, default=1)   
    parser.add_argument('--test_clips', type=int, default=1) 
    parser.add_argument('--dense', default=False, action="store_true",
                    help='use dense sample for test as in Non-local I3D')
    args = parser.parse_args()
    return args

def update_dict(dict):
    new_dict = {}
    for k, v in dict.items():
        new_dict[k.replace('module.', '')] = v
    return new_dict


def main(args):
    init_distributed_mode(args)

    with open(args.config, 'r') as f:
        config = yaml.load(f, Loader=yaml.FullLoader)

    config = DotMap(config)

    device = "cuda"
    if torch.cuda.is_available():
        device = "cuda"
        cudnn.benchmark = True

    # get fp16 model and weight
    model, clip_state_dict = clip.load(
        config.network.arch,
        device='cuda', jit=False,
        internal_modeling=config.network.tm,
        T=config.data.num_segments,
        dropout=config.network.drop_out,
        emb_dropout=config.network.emb_dropout,
        pretrain=config.network.init,
        joint_st= config.network.joint_st) # Must set jit=False for training  ViT-B/32

    video_head = video_header(
        config.network.sim_header,
        config.network.interaction,
        clip_state_dict)

    if args.precision == "amp" or args.precision == "fp32":
        model = model.float()


    input_mean = [0.48145466, 0.4578275, 0.40821073]
    input_std = [0.26862954, 0.26130258, 0.27577711]

    # rescale size
    if 'something' in config.data.dataset:
        scale_size = (240, 320)
    else:
        scale_size = 256 if config.data.input_size == 224 else config.data.input_size

    # crop size
    input_size = config.data.input_size

    # control the spatial crop
    if args.test_crops == 1: # one crop
        cropping = torchvision.transforms.Compose([
            GroupScale(scale_size),
            GroupCenterCrop(input_size),
        ])
    elif args.test_crops == 3:  # do not flip, so only 3 crops (left right center)
        cropping = torchvision.transforms.Compose([
            GroupFullResSample(
                crop_size=input_size,
                scale_size=scale_size,
                flip=False)
        ])
    elif args.test_crops == 5:  # do not flip, so only 5 crops
        cropping = torchvision.transforms.Compose([
            GroupOverSample(
                crop_size=input_size,
                scale_size=scale_size,
                flip=False)
        ])
    elif args.test_crops == 10:
        cropping = torchvision.transforms.Compose([
            GroupOverSample(
                crop_size=input_size,
                scale_size=scale_size,
            )
        ])
    else:
        raise ValueError("Only 1, 3, 5, 10 crops are supported while we got {}".format(args.test_crops))


    val_data = Video_dataset(
        config.data.val_root, config.data.val_list, config.data.label_list,
        random_shift=False, num_segments=config.data.num_segments,
        modality=config.data.modality,
        image_tmpl=config.data.image_tmpl,
        test_mode=True,
        transform=torchvision.transforms.Compose([
            cropping,
            Stack(roll=False),
            ToTorchFormatTensor(div=True),
            GroupNormalize(input_mean, input_std),
        ]),
        dense_sample=args.dense,
        test_clips=args.test_clips)

    # val_sampler = torch.utils.data.distributed.DistributedSampler(val_data)
    val_sampler=None
    val_loader = DataLoader(val_data,
        batch_size=1, num_workers=2,
        sampler=val_sampler, pin_memory=True, drop_last=False)

    if os.path.isfile(args.weights):
        checkpoint = torch.load(args.weights, map_location='cpu',weights_only=True)
        # if dist.get_rank() == 0:
        #     print('load model: epoch {}'.format(checkpoint['epoch']))

        model.load_state_dict(update_dict(checkpoint['model_state_dict']))
        video_head.load_state_dict(update_dict(checkpoint['fusion_model_state_dict']))
        del checkpoint

    if args.distributed:
        model = DistributedDataParallel(model.cuda(), device_ids=[args.gpu], find_unused_parameters=True)
        if config.network.sim_header != "None":
            video_head = DistributedDataParallel(video_head.cuda(), device_ids=[args.gpu])

    classes = text_prompt(val_data)
    n_class = classes.size(0)

    prec1 = validate(
        val_loader, classes, device,
        model, video_head, config, n_class, args.test_crops, args.test_clips)
    return


def validate(val_loader, classes, device, model, video_head, config, n_class, test_crops, test_clips):
    top1 = AverageMeter()
    top5 = AverageMeter()
    model.eval()
    video_head.eval()
    proc_start_time = time.time()
    sim_logits = []   
    labels = []   
    with torch.no_grad():
        text_inputs = classes.to(device)
        # cls_feature, text_features = model.module.encode_text(text_inputs, return_token=True)
        cls_feature, text_features = model.encode_text(text_inputs, return_token=True)

        for i, (image, class_id) in enumerate(val_loader):
            batch_size = class_id.numel()
            num_crop = test_crops

            num_crop *= test_clips  # 4 clips for testing when using dense sample

            class_id = class_id.to(device)
            n_seg = config.data.num_segments
            image = image.view((-1, n_seg, 3) + image.size()[-2:])
            b, t, c, h, w = image.size()
            image_input = image.to(device).view(-1, c, h, w)
            # image_features = model.module.encode_image(image_input).view(b, t, -1)
            image_features = model.encode_image(image_input).view(b, t, -1)
            cnt_time = time.time() - proc_start_time
            similarity = video_head(image_features, text_features, cls_feature)  # b dim
            similarity = F.softmax(similarity, -1)
            similarity = similarity.reshape(batch_size, num_crop, -1).mean(1)  # bs dim

            similarity = similarity.view(batch_size, -1, n_class).softmax(dim=-1)
            similarity = similarity.mean(dim=1, keepdim=False)
            _, predicted_class = torch.max(similarity, dim=1)

            # Print the predicted class
            print(f"Predicted class for batch {i}: {predicted_class.cpu().numpy()}")

    #         if 'anet' in config.data.dataset:
    #             ########## for saving 
    #             sim_logits.append(concat_all_gather(similarity))
    #             labels.append(concat_all_gather(class_id))
    #             ##########

    #         prec = accuracy(similarity, class_id, topk=(1, 5))
    #         prec1 = reduce_tensor(prec[0])
    #         prec5 = reduce_tensor(prec[1])

    #         top1.update(prec1.item(), class_id.size(0))
    #         top5.update(prec5.item(), class_id.size(0))


    #         runtime = float(cnt_time) / (i+1) / (batch_size * 1)
    #         print(
    #             ('Test: [{0}/{1}], average {runtime:.4f} sec/video \t'
    #               'Prec@1 {top1.val:.3f} ({top1.avg:.3f})\t'
    #               'Prec@5 {top5.val:.3f} ({top5.avg:.3f})'.format(
    #                 i, len(val_loader), runtime=runtime, top1=top1, top5=top5)))

    # # if dist.get_rank() == 0:
    # print('-----Evaluation is finished------')
    # print('Overall Prec@1 {:.03f}% Prec@5 {:.03f}%'.format(top1.avg, top5.avg))

    if 'anet' in config.data.dataset:
        sim, gt = sim_logits[0], labels[0]
        for i in range(1, len(sim_logits)): 
            sim = torch.cat((sim, sim_logits[i]), 0)
            gt = torch.cat((gt, labels[i]), 0)

        if dist.get_rank() == 0:
            from utils.utils import mean_average_precision
            mAP = mean_average_precision(sim, gt)
            print('Overall mAP: {:.03f}%'.format(mAP[1].item()))

    return top1.avg

# utils
@torch.no_grad()
def concat_all_gather(tensor):
    """
    Performs all_gather operation on the provided tensors.
    *** Warning ***: torch.distributed.all_gather has no gradient.
    """
    tensors_gather = [torch.ones_like(tensor)
        for _ in range(torch.distributed.get_world_size())]
    torch.distributed.all_gather(tensors_gather, tensor, async_op=False)

    output = torch.cat(tensors_gather, dim=0)
    return output.cpu()


if __name__ == '__main__':
    args = get_parser()
    main(args)

