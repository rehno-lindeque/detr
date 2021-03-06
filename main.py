# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved
import argparse
import datetime
import json
import random
import time
from pathlib import Path

import numpy as np
import torch
from torch.utils.data import DataLoader, DistributedSampler

import datasets
import util.misc as utils
from datasets import build_dataset, get_coco_api_from_dataset
from engine import evaluate, train_one_epoch
from models import build_model


from PIL import Image
import wandb

# Surpress imagebomb error from PIL
Image.MAX_IMAGE_PIXELS = 200000000

# Hard-coded class labels
wandb_class_labels = { 0: "no_object0", 1: "part", 2: "no_object2" }

# Initialize wandb
wandb.init(
  project="detr-experiment"
)

def get_args_parser():
    parser = argparse.ArgumentParser('Set transformer detector', add_help=False)
    parser.add_argument('--lr', default=1e-4, type=float)
    parser.add_argument('--lr_backbone', default=1e-5, type=float)
    parser.add_argument('--batch_size', default=2, type=int)
    parser.add_argument('--weight_decay', default=1e-4, type=float)
    parser.add_argument('--epochs', default=300, type=int)
    parser.add_argument('--lr_drop', default=200, type=int)
    parser.add_argument('--clip_max_norm', default=0.1, type=float,
                        help='gradient clipping max norm')

    # Model parameters
    parser.add_argument('--frozen_weights', type=str, default=None,
                        help="Path to the pretrained model. If set, only the mask head will be trained")
    # * Backbone
    parser.add_argument('--backbone', default='resnet50', type=str,
                        help="Name of the convolutional backbone to use")
    parser.add_argument('--dilation', action='store_true',
                        help="If true, we replace stride with dilation in the last convolutional block (DC5)")
    parser.add_argument('--position_embedding', default='sine', type=str, choices=('sine', 'learned'),
                        help="Type of positional embedding to use on top of the image features")

    # * Transformer
    parser.add_argument('--enc_layers', default=6, type=int,
                        help="Number of encoding layers in the transformer")
    parser.add_argument('--dec_layers', default=6, type=int,
                        help="Number of decoding layers in the transformer")
    parser.add_argument('--dim_feedforward', default=2048, type=int,
                        help="Intermediate size of the feedforward layers in the transformer blocks")
    parser.add_argument('--hidden_dim', default=256, type=int,
                        help="Size of the embeddings (dimension of the transformer)")
    parser.add_argument('--dropout', default=0.1, type=float,
                        help="Dropout applied in the transformer")
    parser.add_argument('--nheads', default=8, type=int,
                        help="Number of attention heads inside the transformer's attentions")
    parser.add_argument('--num_queries', default=100, type=int,
                        help="Number of query slots")
    parser.add_argument('--pre_norm', action='store_true')

    # * Segmentation
    parser.add_argument('--masks', action='store_true',
                        help="Train segmentation head if the flag is provided")

    # Loss
    parser.add_argument('--no_aux_loss', dest='aux_loss', action='store_false',
                        help="Disables auxiliary decoding losses (loss at each layer)")
    # * Matcher
    parser.add_argument('--set_cost_class', default=1, type=float,
                        help="Class coefficient in the matching cost")
    parser.add_argument('--set_cost_bbox_coordinates', default=5, type=float,
                        help="L1 box coefficient in the matching cost on center coordinates")
    parser.add_argument('--set_cost_bbox_dimensions', default=1, type=float,
                        help="L1 box coefficient in the matching cost on box width/height dimensions")
    parser.add_argument('--set_cost_giou', default=2, type=float,
                        help="giou box coefficient in the matching cost")
    # * Loss coefficients
    parser.add_argument('--mask_loss_coef', default=1, type=float)
    parser.add_argument('--dice_loss_coef', default=1, type=float)
    parser.add_argument('--bbox_coordinates_loss_coef', default=5, type=float)
    parser.add_argument('--bbox_dimensions_loss_coef', default=1, type=float)
    parser.add_argument('--giou_loss_coef', default=2, type=float)
    parser.add_argument('--eos_coef', default=0.1, type=float,
                        help="Relative classification weight of the no-object class")

    # dataset parameters
    parser.add_argument('--dataset_file', default='coco')
    parser.add_argument('--coco_path', type=str)
    parser.add_argument('--coco_panoptic_path', type=str)
    parser.add_argument('--remove_difficult', action='store_true')

    parser.add_argument('--output_dir', default='',
                        help='path where to save, empty for no saving')
    parser.add_argument('--device', default='cuda',
                        help='device to use for training / testing')
    parser.add_argument('--seed', default=42, type=int)
    parser.add_argument('--resume', default='', help='resume from checkpoint')
    parser.add_argument('--start_epoch', default=0, type=int, metavar='N',
                        help='start epoch')
    parser.add_argument('--eval', action='store_true')
    parser.add_argument('--num_workers', default=2, type=int)

    # distributed training parameters
    parser.add_argument('--world_size', default=1, type=int,
                        help='number of distributed processes')
    parser.add_argument('--dist_url', default='env://', help='url used to set up distributed training')
    return parser

def coco_annotation_to_wandb_bbox(ann, orig_size):
    bbox = ann["bbox"]
    category_id = ann["category_id"]
    category_label = None
    if category_id in wandb_class_labels.keys():
        category_label = wandb_class_labels[category_id]
    else:
        category_label = str(category_id)
    wandb_bbox = {
        "position": {
            "minX": float(bbox[0]) / orig_size[1],
            "maxX": float(bbox[0] + bbox[2])/ orig_size[1],
            "minY": float(bbox[1]) / orig_size[0],
            "maxY": float(bbox[1] + bbox[3]) / orig_size[0]
        },
        "class_id": ann["category_id"],
        "box_caption" : "%d %s" % (int(ann["id"]), category_label),
        "scores" : {
        },
        "domain" : "percentage"
    }
    return wandb_bbox

def pytorch_box_to_wandb_bbox(box,box_id,category_id,prefix="",score=0):
    category_label = None
    category_id = int(category_id)
    if category_id in wandb_class_labels.keys():
        category_label = wandb_class_labels[category_id]
    else:
        category_label = str(category_id)
    score_caption = ""
    if score != 0:
        score_caption = " (%.2f)" % float(score)
    wandb_bbox = {
        "position": {
            # "minX": max(box[0] - (box[2] * 0.5), 0),
            # "maxX": min(box[0] + (box[2] * 0.5), 1),
            # "minY": max(box[1] - (box[3] * 0.5), 0),
            # "maxY": min(box[1] + (box[3] * 0.5), 1)
            "middle": [float(box[0]), float(box[1])],
            "width": float(box[2]),
            "height": float(box[3]),
        },
        "class_id": int(category_id),
        "box_caption" : "%s%d %s%s" % (prefix, int(box_id), category_label, score_caption),
        "scores" : {
            # "acc": 0.1,
            "score": float(score)
        },
        "domain" : "percentage"
    }
    return wandb_bbox

class WandbEvaluator(object):
    def __init__(self, epoch):
        self.batch_counter = 0
        self.epoch = epoch

    def send(self, targets, results, samples):
        # if (self.epoch % 5) != 0:
        #     return
        if (self.batch_counter % 50) != 0:
            self.batch_counter += 1
            return
        images,mask = samples.decompose()

        # for target in targets:
        wandb_images = []
        for image, target,result in zip(images,targets,results):
            box_data_gt = []
            box_data_dt = []
            # ground truth
            k = 0
            for box, label in zip(target["boxes"], target["labels"]):
                wandb_box = pytorch_box_to_wandb_bbox(box,k,label,prefix="gt",score=0)
                box_data_gt.append(wandb_box)
                k += 1
            # predictions
            k = 0
            for box, label, score in zip(result["boxes"], result["labels"], result["scores"]):
                wandb_box = pytorch_box_to_wandb_bbox(box,k,label,prefix="dt",score=score)
                box_data_dt.append(wandb_box)
                k += 1
            wandb_images.append(wandb.Image(image, boxes = {
                "ground_truth": { "box_data": box_data_gt, "class_labels": wandb_class_labels },
                "predictions": { "box_data": box_data_dt, "class_labels": wandb_class_labels }
            }))
        validation_images = {}
        validation_images[f'epoch_{self.epoch}_batch_{self.batch_counter}'] = wandb_images
        wandb.log(validation_images)

        for (target,result) in zip(targets,results):
            print("samples from image id #", int(target["image_id"]), " with ", target["boxes"].size()[0], "ground truth boxes and ", result["boxes"].size()[0], " predicted boxes" )
        print("batch step counter", self.batch_counter)
        self.batch_counter += 1

def main(args):
    utils.init_distributed_mode(args)
    print("git:\n  {}\n".format(utils.get_sha()))

    if args.frozen_weights is not None:
        assert args.masks, "Frozen training is meant for segmentation only"
    print(args)

    device = torch.device(args.device)

    # fix the seed for reproducibility
    seed = args.seed + utils.get_rank()
    torch.manual_seed(seed)
    np.random.seed(seed)
    random.seed(seed)

    model, criterion, postprocessors = build_model(args)
    model.to(device)

    model_without_ddp = model
    if args.distributed:
        model = torch.nn.parallel.DistributedDataParallel(model, device_ids=[args.gpu])
        model_without_ddp = model.module
    n_parameters = sum(p.numel() for p in model.parameters() if p.requires_grad)
    print('number of params:', n_parameters)

    param_dicts = [
        {"params": [p for n, p in model_without_ddp.named_parameters() if "backbone" not in n and p.requires_grad]},
        {
            "params": [p for n, p in model_without_ddp.named_parameters() if "backbone" in n and p.requires_grad],
            "lr": args.lr_backbone,
        },
    ]
    optimizer = torch.optim.AdamW(param_dicts, lr=args.lr,
                                  weight_decay=args.weight_decay)
    lr_scheduler = torch.optim.lr_scheduler.StepLR(optimizer, args.lr_drop)

    dataset_train = build_dataset(image_set='train', duplication_factor=200, args=args)
    dataset_val = build_dataset(image_set='val', duplication_factor=10, args=args)

    dataset_train_size = len(dataset_train)
    dataset_val_size = len(dataset_train)
    print("training dataset size: ", dataset_train_size)

    if args.distributed:
        sampler_train = DistributedSampler(dataset_train)
        sampler_val = DistributedSampler(dataset_val, shuffle=True)
    else:
        sampler_train = torch.utils.data.RandomSampler(dataset_train)
        sampler_val = torch.utils.data.SequentialSampler(dataset_val)

    batch_sampler_train = torch.utils.data.BatchSampler(
        sampler_train, args.batch_size, drop_last=True)

    data_loader_train = DataLoader(dataset_train, batch_sampler=batch_sampler_train,
                                   collate_fn=utils.collate_fn, num_workers=args.num_workers)
    data_loader_val = DataLoader(dataset_val, args.batch_size, sampler=sampler_val,
                                 drop_last=False, collate_fn=utils.collate_fn, num_workers=args.num_workers)

    if args.dataset_file == "coco_panoptic":
        # We also evaluate AP during panoptic training, on original coco DS
        coco_val = datasets.coco.build("val", args)
        base_ds = get_coco_api_from_dataset(coco_val)
    else:
        base_ds = get_coco_api_from_dataset(dataset_val)

    if args.frozen_weights is not None:
        checkpoint = torch.load(args.frozen_weights, map_location='cpu')
        model_without_ddp.detr.load_state_dict(checkpoint['model'])

    output_dir = Path(args.output_dir)
    if args.resume:
        if args.resume.startswith('https'):
            checkpoint = torch.hub.load_state_dict_from_url(
                args.resume, map_location='cpu', check_hash=True)
        else:
            checkpoint = torch.load(args.resume, map_location='cpu')
        model_without_ddp.load_state_dict(checkpoint['model'])
        if not args.eval and 'optimizer' in checkpoint and 'lr_scheduler' in checkpoint and 'epoch' in checkpoint:
            optimizer.load_state_dict(checkpoint['optimizer'])
            lr_scheduler.load_state_dict(checkpoint['lr_scheduler'])
            args.start_epoch = checkpoint['epoch'] + 1

    if args.eval:
        test_stats, coco_evaluator = evaluate(model, criterion, postprocessors,
                                              data_loader_val, base_ds, device, args.output_dir, WandbEvaluator(epoch), epoch, num_batches=(dataset_train_size // args.batch_size) )
        if args.output_dir:
            utils.save_on_master(coco_evaluator.coco_eval["bbox"].eval, output_dir / "eval.pth")
        return

    # wandb monitor model during training
    wandb.config.update(args)
    # wandb.watch(model)

    print("Start training")
    start_time = time.time()
    for epoch in range(args.start_epoch, args.epochs):
        if args.distributed:
            sampler_train.set_epoch(epoch)
        train_stats = train_one_epoch(
            model, criterion, data_loader_train, optimizer, device, epoch, num_batches=(dataset_train_size // args.batch_size),
            max_norm=args.clip_max_norm,
            postprocessors=postprocessors,
            wandb_evaluator=WandbEvaluator(epoch)
            )
        lr_scheduler.step()
        if args.output_dir:
            checkpoint_paths = [output_dir / 'checkpoint.pth']
            # extra checkpoint before LR drop and every 100 epochs
            if (epoch + 1) % args.lr_drop == 0 or (epoch + 1) % 100 == 0:
                checkpoint_paths.append(output_dir / f'checkpoint{epoch:04}.pth')
            for checkpoint_path in checkpoint_paths:
                utils.save_on_master({
                    'model': model_without_ddp.state_dict(),
                    'optimizer': optimizer.state_dict(),
                    'lr_scheduler': lr_scheduler.state_dict(),
                    'epoch': epoch,
                    'args': args,
                }, checkpoint_path)

        test_stats, coco_evaluator = evaluate(
            model, criterion, postprocessors, data_loader_val, base_ds, device, args.output_dir, WandbEvaluator(epoch), epoch, num_batches=(dataset_val_size // args.batch_size)
        )

        log_stats = {**{f'test_{k}': v for k, v in test_stats.items()},
                     'epoch': epoch,
                     'n_parameters': n_parameters}
        wandb.log(log_stats)

    total_time = time.time() - start_time
    total_time_str = str(datetime.timedelta(seconds=int(total_time)))
    print('Training time {}'.format(total_time_str))


if __name__ == '__main__':
    parser = argparse.ArgumentParser('DETR training and evaluation script', parents=[get_args_parser()])
    args = parser.parse_args()
    if args.output_dir:
        Path(args.output_dir).mkdir(parents=True, exist_ok=True)
    main(args)
