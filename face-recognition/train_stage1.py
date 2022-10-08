#!/usr/bin/env python
# encoding: utf-8
'''
@author: wujiyang
@contact: wujiyang@hust.edu.cn
@file: train.py.py
@time: 2018/12/21 17:37
@desc: train script for deep face recognition
'''

import os
import pathlib
base_folder = str(pathlib.Path(__file__).parent.resolve())
os.chdir(base_folder)
import torch.utils.data
from torch.nn import DataParallel
from datetime import datetime
from backbone.irevnet import iRevNet
from backbone.iresnet import iresnet50, iresnet100
from backbone.resnet import ResNet
from margin.ArcMarginProduct import ArcMarginProduct
from margin.MultiMarginProduct import MultiMarginProduct
from margin.CosineMarginProduct import CosineMarginProduct
from margin.InnerProduct import InnerProduct
# from utility.visualize import Visualizer
from utility.log import init_log
from dataset.casia_webface import CASIAWebFace
from dataset.agedb import AgeDB30
from dataset.cfp import CFP_FP
from torch.optim import lr_scheduler
import torch.optim as optim
import torch.nn as nn
import time
from evaluation.eval_lfw import evaluation_10_fold, getFeatureFromTorch
import numpy as np
import torchvision.transforms as transforms
import argparse
from tqdm import tqdm
import torch.nn.functional as F
import random


def set_random_seed(seed_value, use_cuda=True):
    np.random.seed(seed_value) # cpu vars
    torch.manual_seed(seed_value) # cpu  vars
    random.seed(seed_value) # Python
    os.environ['PYTHONHASHSEED'] = str(seed_value) # Python hash buildin
    if use_cuda: 
        torch.cuda.manual_seed(seed_value)
        torch.cuda.manual_seed_all(seed_value) # gpu vars
        torch.backends.cudnn.deterministic = True  #needed
        torch.backends.cudnn.benchmark = False
        
        
def un_normalize(image, mu=torch.tensor([0.5, 0.5, 0.5]).float(), std=torch.tensor([0.5, 0.5, 0.5]).float()):
    device = image.device
    image = image.permute(0, 2, 3, 1) * std.to(device) + mu.to(device)
    image = image.permute(0, 3, 1, 2)
    return image
    
    
def train(args):
    # gpu init
    multi_gpus = False
    if len(args.gpus.split(',')) > 1:
        multi_gpus = True
    os.environ['CUDA_VISIBLE_DEVICES'] = args.gpus
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

    # log init
    save_dir = args.save_dir
    os.makedirs(save_dir, exist_ok=True)
    logging = init_log(save_dir)
    _print = logging.info

    # dataset loader
    transform = transforms.Compose([
        transforms.ToTensor(),  # range [0, 255] -> [0.0,1.0]
        transforms.Normalize(mean=(0.5, 0.5, 0.5), std=(0.5, 0.5, 0.5))  # range [0.0, 1.0] -> [-1.0,1.0]
    ])

    # validation dataset
    trainset = CASIAWebFace(args.train_root, args.train_file_list, args.down_size, transform=transform)
    trainloader = torch.utils.data.DataLoader(trainset, batch_size=args.batch_size,
                                              shuffle=True, num_workers=4, drop_last=False)
    

    # define backbone and margin layer
    if args.backbone == 'iresnet50':
        net1 = iresnet50(attention_type=args.mode)
    elif args.backbone == 'iresnet100':
        net1 = iresnet100(attention_type=args.mode)
    elif args.backbone == 'resnet50':
        net1 = ResNet(num_layers=50, mode=args.mode)
    else:
        raise('Select Proper Backbone Network')
    
    
    net2 = iRevNet(nBlocks=[6, 16, 72, 6], nStrides=[2, 2, 2, 1],
                  nChannels=[24, 96, 384, 1536], nClasses=1000, init_ds=2,
                  dropout_rate=0., affineBN=True, in_shape=[3, 112, 112], mult=4)

    if args.margin_type == 'ArcFace':
        margin = ArcMarginProduct(args.feature_dim, trainset.class_nums, s=args.scale_size)
    elif args.margin_type == 'MultiMargin':
        margin = MultiMarginProduct(args.feature_dim, trainset.class_nums, s=args.scale_size)
    elif args.margin_type == 'CosFace':
        margin = CosineMarginProduct(args.feature_dim, trainset.class_nums, s=args.scale_size, m=0.25)
    elif args.margin_type == 'Softmax':
        margin = InnerProduct(args.feature_dim, trainset.class_nums)
    elif args.margin_type == 'SphereFace':
        pass
    else:
        print(args.margin_type, 'is not available!')

    
    # define optimizers for different layer
    criterion = torch.nn.CrossEntropyLoss().to(device)
    optimizer_ft = optim.SGD([
        {'params': net1.parameters(), 'weight_decay': 5e-4},
        {'params': margin.parameters(), 'weight_decay': 5e-4},
        {'params': net2.parameters(), 'weight_decay': 5e-4}
    ], lr=0.1, momentum=0.9, nesterov=True)
    exp_lr_scheduler = lr_scheduler.MultiStepLR(optimizer_ft, milestones=[18000, 28000, 36000, 44000], \
                                                gamma=0.1)

    if multi_gpus:
        net1 = DataParallel(net1).to(device)
        net2 = DataParallel(net2).to(device)
        margin = DataParallel(margin).to(device)
    else:
        net1 = net1.to(device)
        net2 = net2.to(device)
        margin = margin.to(device)

    total_iters = 0

    # Run
    GOING = True
    while GOING:
        # train model
        net1.train()
        net2.train()
        margin.train()

        since = time.time()
        for data in tqdm(trainloader):            
            run['result/iters'].log(total_iters)
            HR_img, label = data[0].to(device), data[2].to(device)                  
                        
            raw_logits, out_bij = net1(HR_img, train=True)
            out = margin(raw_logits, label)
            
            HR_img_gen = net2.inverse(out_bij)
            
            # Loss
            cri_loss = criterion(out, label)
            recon_loss = F.l1_loss(torch.clip(HR_img_gen, 0, 1), un_normalize(HR_img))
            total_loss = cri_loss + recon_loss * (1.0)


            # Optim
            optimizer_ft.zero_grad()
            total_loss.backward()
            optimizer_ft.step()


            # print train information
            if total_iters % 100 == 0:
                # current training accuracy
                _, predict = torch.max(out.data, 1)
                total = label.size(0)

                correct = (np.array(predict.cpu()) == np.array(label.data.cpu())).sum()
                time_cur = (time.time() - since) / 100
                since = time.time()
                run['result/train_loss'].log(total_loss.item())
                run['result/train_accuracy'].log(correct/total)
                _print("Iters: {:0>6d}, loss: {:.4f}, train_accuracy: {:.4f}, time: {:.2f} s/iter, learning rate: {}".format(total_iters, total_loss.item(), correct/total, time_cur, exp_lr_scheduler.get_lr()[0]))


            # save model
            if total_iters % args.save_freq == 0:
                msg = 'Saving checkpoint: {}'.format(total_iters)
                _print(msg)
                if multi_gpus:
                    net1_state_dict = net1.module.state_dict()
                    net2_state_dict = net2.module.state_dict()
                    margin_state_dict = margin.module.state_dict()
                else:
                    net1_state_dict = net1.state_dict()
                    net2_state_dict = net2.state_dict()
                    margin_state_dict = margin.state_dict()
                    
                if not os.path.exists(save_dir):
                    os.mkdir(save_dir)
                    
                torch.save({
                    'iters': total_iters,
                    'net1_state_dict': net1_state_dict,
                    'net2_state_dict': net2_state_dict
                    },
                    os.path.join(save_dir, 'Iter_%06d_net.ckpt' % total_iters))
                
                torch.save({
                    'iters': total_iters,
                    'net_state_dict': margin_state_dict},
                    os.path.join(save_dir, 'Iter_%06d_margin.ckpt' % total_iters))
            
            
            run['debug/lr'].log(exp_lr_scheduler.get_lr()[0])
            exp_lr_scheduler.step()
            
            # Stop
            if total_iters == 47000:
                GOING = False
                break
        
            # Next Iterations
            total_iters += 1
            
            

    # Save Last Epoch
    msg = 'Saving checkpoint: {}'.format(total_iters)
    _print(msg)
    if multi_gpus:
        net1_state_dict = net1.module.state_dict()
        net2_state_dict = net2.module.state_dict()
        margin_state_dict = margin.module.state_dict()
    else:
        net1_state_dict = net1.state_dict()
        net2_state_dict = net2.state_dict()
        margin_state_dict = margin.state_dict()
        
    torch.save({
        'iters': total_iters,
        'net1_state_dict': net1_state_dict,
        'net2_state_dict': net2_state_dict
        },
        os.path.join(save_dir, 'last_net.ckpt'))
    torch.save({
        'iters': total_iters,
        'net_state_dict': margin_state_dict},
        os.path.join(save_dir, 'last_margin.ckpt'))


    # test dataset
    net1.eval()
    net2.eval()
    margin.eval()
    
    print('Evaluation on LFW, AgeDB-30. CFP')
    os.makedirs(os.path.join(args.save_dir, 'result'), exist_ok=True)
    
    if args.down_size == 1:
        eval_list = [112, 56, 28, 14]
    elif args.down_size == 0:
        eval_list = [112]
    else: 
        eval_list = [args.down_size]
        
    for down_size in eval_list:
        agedbdataset = AgeDB30(args.agedb_test_root, args.agedb_file_list, down_size, transform=transform)
        cfpfpdataset = CFP_FP(args.cfpfp_test_root, args.cfpfp_file_list, down_size, transform=transform)
        
        agedbloader = torch.utils.data.DataLoader(agedbdataset, batch_size=args.batch_size, shuffle=False, num_workers=4, drop_last=False)
        cfpfploader = torch.utils.data.DataLoader(cfpfpdataset, batch_size=args.batch_size, shuffle=False, num_workers=4, drop_last=False)

        # test model on AgeDB30
        getFeatureFromTorch(os.path.join(args.save_dir, 'result/cur_agedb30_result.mat'), net1, device, agedbdataset, agedbloader)
        age_accs = evaluation_10_fold(os.path.join(args.save_dir, 'result/cur_agedb30_result.mat'))
        run['result/age_acc_%d' %down_size].log(np.mean(age_accs) * 100)

        # test model on CFP-FP
        getFeatureFromTorch(os.path.join(args.save_dir, 'result/cur_cfpfp_result.mat'), net1, device, cfpfpdataset, cfpfploader)
        cfp_accs = evaluation_10_fold(os.path.join(args.save_dir, 'result/cur_cfpfp_result.mat'))
        run['result/cfp_acc_%d' %down_size].log(np.mean(cfp_accs) * 100)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='PyTorch for deep face recognition')
    parser.add_argument('--data_dir', type=str, default='/data/sung/dataset/Face')
    parser.add_argument('--save_dir', type=str, default='/data/sung/checkpoint/lr_face_recognition/qualnet_cosface/stage1', help='model save dir')
    parser.add_argument('--down_size', type=int, default=0) # 1 : all type, 0 : high, others : low
    parser.add_argument('--seed', type=int, default=1) # 1 : all type, 0 : high, others : low
    parser.add_argument('--margin_type', type=str, default='CosFace', help='ArcFace, CosFace, SphereFace, MultiMargin, Softmax')
    parser.add_argument('--feature_dim', type=int, default=512, help='feature dimension, 128 or 512')
    parser.add_argument('--backbone', type=str, default='iresnet50')
    parser.add_argument('--mode', type=str, default='ir')
    parser.add_argument('--scale_size', type=float, default=30.0, help='scale size')
    parser.add_argument('--batch_size', type=int, default=256, help='batch size')
    parser.add_argument('--save_freq', type=int, default=10000, help='save frequency')
    parser.add_argument('--gpus', type=str, default='0', help='model prefix')
    args = parser.parse_args()

    # Path
    args.train_root = os.path.join(args.data_dir, 'faces_webface_112x112/image')
    args.train_file_list = os.path.join(args.data_dir, 'faces_webface_112x112/train.list')
    args.lfw_test_root = os.path.join(args.data_dir, 'evaluation/lfw')
    args.lfw_file_list = os.path.join(args.data_dir, 'evaluation/lfw.txt')
    args.agedb_test_root = os.path.join(args.data_dir, 'evaluation/agedb_30')
    args.agedb_file_list = os.path.join(args.data_dir, 'evaluation/agedb_30.txt')
    args.cfpfp_test_root = os.path.join(args.data_dir, 'evaluation/cfp_fp')
    args.cfpfp_file_list = os.path.join(args.data_dir, 'evaluation/cfp_fp.txt')
    
    args.distill_type = 'qualnet_stage1'

    # Set seed
    set_random_seed(args.seed)

    # Logger
    import neptune.new as neptune
    monitor_hardware = True
    mode = 'async'
    token = 'eyJhcGlfYWRkcmVzcyI6Imh0dHBzOi8vYXBwLm5lcHR1bmUuYWkiLCJhcGlfdXJsIjoiaHR0cHM6Ly9hcHAubmVwdHVuZS5haSIsImFwaV9rZXkiOiI5MTQ3MjY2Yy03YmM4LTRkOGYtOWYxYy0zOTk3MWI0ZDY3M2MifQ=='
    run = neptune.init('sunghoshin/CVPR-Face', api_token=token,
                capture_stdout=monitor_hardware,
                capture_stderr=monitor_hardware,
                capture_hardware_metrics=monitor_hardware,
                mode=mode
                )

    for key, value in vars(args).items():
        run[key] = value
    
    run['task'] = 'face_recognition_qualnet_stage1'
    train(args)