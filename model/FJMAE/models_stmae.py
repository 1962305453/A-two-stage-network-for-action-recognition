# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.

# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.
# --------------------------------------------------------
# References:
# timm: https://github.com/rwightman/pytorch-image-models/tree/master/timm
# DeiT: https://github.com/facebookresearch/deit
# --------------------------------------------------------
import torch.nn.functional as F
import math
from functools import partial
from unittest.mock import patch
from transformer_utils import Block1, CrossAttentionBlock
import torch
import torch.nn as nn
import warnings
from timm.models.vision_transformer import Block

import numpy as np
#from util.pos_embed import get_2d_sincos_pos_embed
from util.skeleton_Embed import SkeleEmbed
from util.sklearn_embed import PatchEmbed,get_2d_sincos_pos_embed
def trunc_normal_(tensor, mean=0., std=1., a=-2., b=2.):
    ## type: (Tensor, float, float, float, float) -> Tensor
    r"""Fills the input Tensor with values drawn from a truncated
    normal distribution. The values are effectively drawn from the
    normal distribution :math:`\mathcal{N}(\text{mean}, \text{std}^2)`
    with values outside :math:`[a, b]` redrawn until they are within
    the bounds. The method used for generating the random values works
    best when :math:`a \leq \text{mean} \leq b`.
    Args:
        tensor: an n-dimensional `torch.Tensor`
        mean: the mean of the normal distribution
        std: the standard deviation of the normal distribution
        a: the minimum cutoff value
        b: the maximum cutoff value
    Examples:
        >>> w = torch.empty(3, 5)
        >>> nn.init.trunc_normal_(w)
    """
    return _no_grad_trunc_normal_(tensor, mean, std, a, b)
def _no_grad_trunc_normal_(tensor, mean, std, a, b):
    # Cut & paste from PyTorch official master until it's in a few official releases - RW
    # Method based on https://people.sc.fsu.edu/~jburkardt/presentations/truncated_normal.pdf
    def norm_cdf(x):
        # Computes standard normal cumulative distribution function
        return (1. + math.erf(x / math.sqrt(2.))) / 2.

    if (mean < a - 2 * std) or (mean > b + 2 * std):
        warnings.warn("mean is more than 2 std from [a, b] in nn.init.trunc_normal_. "
                      "The distribution of values may be incorrect.",
                      stacklevel=2)

    with torch.no_grad():
        # Values are generated by using a truncated uniform distribution and
        # then using the inverse CDF for the normal distribution.
        # Get upper and lower cdf values
        l = norm_cdf((a - mean) / std)
        u = norm_cdf((b - mean) / std)

        # Uniformly fill tensor with values from [l, u], then translate to
        # [2l-1, 2u-1].
        tensor.uniform_(2 * l - 1, 2 * u - 1)

        # Use inverse cdf transform for normal distribution to get truncated
        # standard normal
        tensor.erfinv_()

        # Transform to proper mean, std
        tensor.mul_(std * math.sqrt(2.))
        tensor.add_(mean)

        # Clamp to ensure it's in the proper range
        tensor.clamp_(min=a, max=b)
        return tensor
class MaskedAutoencoderViT(nn.Module):
    """ Masked Autoencoder with VisionTransformer backbone
    """

    def __init__(self,frame_nums=120,skeleton_nums=18, in_chans=3,patch_size=1, t_patch_size=4,
                 embed_dim=256, depth=8, num_heads=8,dim_feat=256,
                 decoder_embed_dim=256, decoder_depth=6, decoder_num_heads=8,
                 mlp_ratio=4., drop_rate=0.,norm_layer=nn.LayerNorm, norm_pix_loss=True):
        super().__init__()
        
        # -------------- patch_size=4, embed_dim=16, depth=8, num_heads=4,
        #decoder_embed_dim=8, decoder_depth=6, decoder_num_heads=4,
        #mlp_ratio=4, norm_layer=partial(nn.LayerNorm, eps=1e-6), **kwargs)------------------------------------------------------------
        # ST-MAE encoder specifics
        self.dim_feat = dim_feat
        self.num_frames = frame_nums
        self.num_joints = skeleton_nums
        self.patch_size = patch_size
        self.t_patch_size = t_patch_size
        # self.norm_skes_loss = norm_skes_loss
        # self.patch_embed = PatchEmbed(frame_nums,skeleton_nums,patch_size,embed_dim)
        self.joints_embed = SkeleEmbed(in_chans, dim_feat, frame_nums, skeleton_nums, patch_size, t_patch_size)
        # num_patches = self.patch_embed.num_patches
        self.temp_embed = nn.Parameter(torch.zeros(1, frame_nums//t_patch_size, 1, dim_feat))
        self.pos_embed = nn.Parameter(torch.zeros(1, 1, skeleton_nums//patch_size, dim_feat))
        self.cls_token = nn.Parameter(torch.zeros(1, 1, embed_dim))
        # trunc_normal_(self.temp_embed, std=.02)
        # trunc_normal_(self.pos_embed, std=.02)
        #1 271 512
        # self.pos_embed = nn.Parameter(torch.zeros(1, num_patches + 1, embed_dim), requires_grad=False)  # fixed sin-cos embedding
        #input x 64 1351 768
        # trunc_normal_(self.mask_token, std=.02)
        self.blocks = nn.ModuleList([
            Block(embed_dim, num_heads, mlp_ratio, qkv_bias=True,  norm_layer=norm_layer)
            for i in range(depth)])
        self.norm = norm_layer(embed_dim)
       
        # --------------------------------------------------------------------------

        # --------------------------------------------------------------------------
        # MAE decoder specifics
        # self.decoder_embed = nn.Linear(embed_dim, decoder_embed_dim, bias=True)
        self.decoder_embed = nn.Linear(dim_feat, decoder_embed_dim, bias=True)
        self.mask_token = nn.Parameter(torch.zeros(1, 1, decoder_embed_dim))
        
        self.decoder_pos_embed = nn.Parameter(torch.zeros(1, 1, decoder_embed_dim), requires_grad=False)  # fixed sin-cos embedding
        self.decoder_pos_embed_mask=nn.parameter(torch.zeros(1,1+self.joints_embed.num_patches,decoder_embed_dim),requires_grad=False)
        self.decoder_blocks = nn.ModuleList([
            CrossAttentionBlock(embed_dim,decoder_embed_dim,decoder_num_heads, mlp_ratio, qkv_bias=True, qk_scale=None, norm_layer=norm_layer)
            for i in range(decoder_depth)])
        self.decoder_norm = norm_layer(decoder_embed_dim)
        self.decoder_temp_embed = nn.Parameter(torch.zeros(1, frame_nums//t_patch_size, 1, decoder_embed_dim))
        self.decoder_pos_embed = nn.Parameter(torch.zeros(1, 1, skeleton_nums//patch_size, decoder_embed_dim))
        trunc_normal_(self.decoder_temp_embed, std=.02)
        trunc_normal_(self.decoder_pos_embed, std=.02)
        trunc_normal_(self.mask_token, std=.02)
        trunc_normal_(self.cls_token, std=.02)
        # self.decoder_pred = nn.Linear(decoder_embed_dim,  1*patch_size*in_chans, bias=True) # decoder to patch
        # --------------------------------------------------------------------------
        self.decoder_pred = nn.Linear(
            decoder_embed_dim,
            t_patch_size * patch_size * in_chans,
            bias=True
        )
        self.norm_pix_loss = norm_pix_loss
        self.apply(self._init_weights)

    # def initialize_weights(self):
    #     # initialization
    #     # initialize (and freeze) pos_embed by sin-cos embedding
    #     #self.pos_embed.shape 1*197*1024
    #     #out 197*1024 (196+1)*1024
    #     pos_embed = get_2d_sincos_pos_embed(self.pos_embed.shape[-1], self.patch_embed.grid_size, cls_token=True)
    #     self.pos_embed.data.copy_(torch.from_numpy(pos_embed).float().unsqueeze(0))

    #     decoder_pos_embed = get_2d_sincos_pos_embed(self.decoder_pos_embed.shape[-1], self.patch_embed.grid_size, cls_token=True)
    #     self.decoder_pos_embed.data.copy_(torch.from_numpy(decoder_pos_embed).float().unsqueeze(0))
    #     # initialize patch_embed like nn.Linear (instead of nn.Conv2d)
    #     w = self.patch_embed.proj.weight.data
    #     torch.nn.init.xavier_uniform_(w.view([w.shape[0], -1]))

    #     # timm's trunc_normal_(std=.02) is effectively normal_(std=0.02) as cutoff is too big (2.)
    #     #高斯分布初始化
    #     torch.nn.init.normal_(self.cls_token, std=.02)
    #     torch.nn.init.normal_(self.mask_token, std=.02)
    #     # initialize nn.Linear and nn.LayerNorm


    def _init_weights(self, m):
        if isinstance(m, nn.Linear):
            # we use xavier_uniform following official JAX ViT:
            torch.nn.init.xavier_uniform_(m.weight)
            if isinstance(m, nn.Linear) and m.bias is not None:
                nn.init.constant_(m.bias, 0)  #权重和偏置
        elif isinstance(m, nn.LayerNorm):
            nn.init.constant_(m.bias, 0)
            nn.init.constant_(m.weight, 1.0)

    def patchify(self, imgs):
        """
        imgs: (N, 3, H, W) 64 3 300 18 2
        x: (N, L, patch_size**2 *3)
        """
        ##把图片划分为块
        #p = self.patch_embed.patch_size[0]#1
        #assert imgs.shape[2] == imgs.shape[3] and imgs.shape[2] % p == 0
        #h = w = imgs.shape[2] // p
        #x = imgs.reshape(shape=(imgs.shape[0], 3, h, p, w, p))#64 3 14 16 14 16
        #x = torch.einsum('nchpwq->nhwpqc', x)
        #x = x.reshape(shape=(imgs.shape[0], h * w, p**2 * 3))  #64 196  16*16*3
        #input:64 3 300 18 2  ->> 64 2 300 18 1
        # N, C, T, V, M = x.size()
        # x = x.permute(0, 4, 3, 1, 2).contiguous()
        # x = x.view(N * M, V * C, T)
        # #x = data_bn(x)
        # x = x.view(N, M, V, C, T)
        # x = x.permute(0, 1, 3, 4, 2).contiguous()
        # x = x.view(N * M, C, T, V)[:,:2,...].unsqueeze(-1)
        NM, T, V, C = imgs.shape
        p = self.patch_size
        u = self.t_patch_size
        assert V % p == 0 and T % u == 0
        VP = V // p
        TP = T // u

        x = imgs.reshape(shape=(NM, TP, u, VP, p, C))
        x = torch.einsum("ntuvpc->ntvupc", x)
        x = x.reshape(shape=(NM, TP * VP, u * p * C))  ## 64  540  12
        # x=x[...,0]
        # #batch 3 19 300
        # x=x.transpose(3,2)
        # h=x.shape[2]
        # #75
        # w=self.patch_embed.grid_size[1]
        # p=int(x.shape[3]/self.patch_embed.grid_size[1])
        # x = x.reshape(shape=(x.shape[0], 3,  h,1,w ,p))
        # x = torch.einsum('nchpwq->nhwpqc', x)
        # x = x.reshape(shape=(x.shape[0], h * w,1 *p* 3))
        # #x=self.norm(x)
        # #64*3*(19*300)
        # # x=self.patch_embed.deproj(x)
        # # x = x.flatten(2)
        # # #64*19*300*16
        # # x=x.transpose(2,1)
        # #x=x[:,:,:,:,0].unsqueeze(-1)
        # #64 18 300 2 1
        # #x=x.transpose(1,3)
        # #64 18 600
        # #x = x.flatten(2)
        return x

    def unpatchify(self, x):
        """
        x: (N, L, patch_size**2 *3) 64 18 600
        imgs: (N, 3, H, W)          64  2 300 18 1
        """
        ### 还原成图片
        # x = x.reshape(shape=(x.shape[0], 18, 300, 2))
      
        # x=x.transpose(1,3)
        # # 64 2 300 18
        # x=x.transpose(2,3).unsqueeze(-1)
        #4
        p = self.patch_embed.patch_size[1]
        h = self.patch_embed.grid_size[0]#19
        w = self.patch_embed.grid_size[1]#75int(x.shape[1]**.5)
        #assert h * w == x.shape[1]
        
        x = x.reshape(shape=(x.shape[0], h, w, 1, p, 3))
        x = torch.einsum('nhwpqc->nchpwq', x)
        #batch 3 19 300
        x = x.reshape(shape=(x.shape[0], 3, h * 1, w * p))
        #bacth 3 300 19
        x=x.transpose(3,2)
      
        return x

    def random_masking(self, x, mask_ratio):
        """
        Perform per-sample random masking by per-sample shuffling.
        Per-sample shuffling is done by argsort random noise.
        x: [N, L, D], sequence
        """
        #imge 64 196 1024
        #     18 
        N, L, D = x.shape  # batch, length, dim
        len_keep = int(L * (1 - mask_ratio))
        
        noise = torch.rand(N, L, device=x.device)  # noise in [0, 1]
        
        # sort noise for each sample
        ids_shuffle = torch.argsort(noise, dim=1)  # ascend: small is keep, large is remove
        ids_restore = torch.argsort(ids_shuffle, dim=1)

        # keep the first subset
        ids_keep = ids_shuffle[:, :len_keep]
        x_masked = torch.gather(x, dim=1, index=ids_keep.unsqueeze(-1).repeat(1, 1, D))

        # generate the binary mask: 0 is keep, 1 is remove
        mask = torch.ones([N, L], device=x.device)
        mask[:, :len_keep] = 0
        # unshuffle to get the binary mask
        mask = torch.gather(mask, dim=1, index=ids_restore)

        return x_masked, mask, ids_restore,ids_keep
    def uniform_masking1(self, x, mask_ratio):
        """
        Perform per-sample random masking by per-sample shuffling.
        Per-sample shuffling is done by argsort random noise.
        x: [N, L, D], sequence
        """
        #imge 64 196 1024
        #     18 
        N, L, D = x.shape  # batch, length, dim
        len_keep = int(L * (1 - mask_ratio))
        #3
        step=round(len_keep/self.grid_size[1])
        even = torch.arange(0,L,step=step).repeat(N,1)#torch.arange(0,L,(N,L),device=x.device) #torch.rand(N, L, device=x.device)  # noise in [0, 1]
        n=1
        next_end=step
        #64 18 15
        for i in range(len_keep) :
            if next_end>L:
                next_end=L
            odd = torch.arange(n,next_end,step=1).repeat(N,1)
            
            even=torch.cat((even,odd),dim=1)
            n+=step
            next_end+=step
        uniform_ids=even.cuda()

        # even = torch.arange(0,L,step=2).repeat(N,1)#torch.arange(0,L,(N,L),device=x.device) #torch.rand(N, L, device=x.device)  # noise in [0, 1]
        # odd = torch.arange(1,L,step=2).repeat(N,1)
        # uniform_ids=torch.cat((even,odd),dim=1).cuda()
        #print(even.shape[1])
        # sort noise for each sample
        #ids_shuffle = torch.argsort(noise, dim=1)  # ascend: small is keep, large is remove
        ids_restore = torch.argsort(uniform_ids, dim=1).cuda()

        # keep the first subset
        ids_keep = uniform_ids[:,:even.shape[1]]#ids_shuffle[:, :len_keep]
        x_masked = torch.gather(x, dim=1, index=ids_keep.unsqueeze(-1).repeat(1, 1, D)).cuda()

        # generate the binary mask: 0 is keep, 1 is remove
        mask = torch.ones([N, L], device=x.device)
        mask[:, :len_keep] = 0
        # unshuffle to get the binary mask
        mask = torch.gather(mask, dim=1, index=ids_restore)

        return x_masked, mask, ids_restore
    def uniform_masking(self, x, mask_ratio):
        """
        Perform per-sample random masking by per-sample shuffling.
        Per-sample shuffling is done by argsort random noise.
        x: [N, L, D], sequence
        """
        #imge 64 196 1024
        #     18 
        N, L, D = x.shape  # batch, length, dim
        len_keep = int(L * (1 - mask_ratio))
        #4
        #self.patch_embed.num_patches
        step=round(len_keep/self.patch_embed.grid_size[1])
        #save#15
        begin=0
        end=self.patch_embed.num_skearn
        even = torch.arange(begin,end,step=step)
        begin=end
        for i in range(1,self.patch_embed.grid_size[1]):
            # if next_end>L:
            #     next_end=L
            end+=self.patch_embed.grid_size[0]
            temp = torch.arange(begin,end,step=step)
            even=torch.cat((even,temp))
            begin=end
        even_numpy=even.numpy()
        numbers=np.arange(0,L,step=1)
        del_nums=list(set(numbers).difference(set(even_numpy)))
        diff_numpy= np.array(del_nums)
        diff_torch=torch.from_numpy(diff_numpy)
        even=torch.cat((even,diff_torch)).repeat(N,1)
        # addlens=even.shape[1]
        # #even = torch.arange(0,L,step=step).repeat(N,1)#torch.arange(0,L,(N,L),device=x.device) #torch.rand(N, L, device=x.device)  # noise in [0, 1]
        # n=1
        # next_end=step
        # #64 18 15
        # for i in range(1,addlens):
        #     if next_end >i* self.patch_embed.num_skearn:
        #         next_end=i*self.patch_embed.num_skearn
        #     odd = torch.arange(n,next_end,step=1).repeat(N,1)
            
        #     even=torch.cat((even,odd),dim=1)
        #     n+=step
        #     next_end+=step
        uniform_ids=even.cuda()
        # even = torch.arange(0,L,step=2).repeat(N,1)#torch.arange(0,L,(N,L),device=x.device) #torch.rand(N, L, device=x.device)  # noise in [0, 1]
        # odd = torch.arange(1,L,step=2).repeat(N,1)
        # uniform_ids=torch.cat((even,odd),dim=1).cuda()
        #print(even.shape[1])
        # sort noise for each sample
        #ids_shuffle = torch.argsort(noise, dim=1)  # ascend: small is keep, large is remove
        ids_restore = torch.argsort(uniform_ids, dim=1).cuda()
        # keep the first subset
        ids_keep = uniform_ids[:,:len_keep]#ids_shuffle[:, :len_keep]
        x_masked = torch.gather(x, dim=1, index=ids_keep.unsqueeze(-1).repeat(1, 1, D)).cuda()
        # generate the binary mask: 0 is keep, 1 is remove
        mask = torch.ones([N, L], device=x.device)
        mask[:, :len_keep] = 0
        # unshuffle to get the binary mask
        mask = torch.gather(mask, dim=1, index=ids_restore)
        return x_masked, mask, ids_restore
    # def weak_skeleton(self,data_skeleton):
    #     B,C, T, V, M =data_skeleton.shape
    #     # data of frame 1
    #     xy1 = data_skeleton[:,0:2, 0:T - 1, :, :]#.reshape(2, T - 1, V, M, 1)
    #     # data of frame 2
    #     xy2 =data_skeleton[:,0:2, 1:T, :, :]#.reshape(2, T - 1, V, 1, M)
    #     # square of distance between frame 1&2 M:0
    #     distance = ((xy2 - xy1)**2).sum(axis=2).sum(axis=1)[...,0]
    #     # a weak point in a sequence of movements
    #     ids_skeleton= distance.argsort() 

    #     #skeleton is ids sequence

    #     return ids_skeleton

    # def dynamic_masking(self,x,data_skeleton,mask_ratio):
    #         """
    #         Perform per-sample random masking by per-sample shuffling.
    #         Per-sample shuffling is done by argsort random noise.
    #         x: [N, L, D], sequence
    #         """
    #         #imge 64 196 1024
    #         #     18 
    #         N, L, D = x.shape  # batch, length, dim
    #         len_keep = int(L * (1 - mask_ratio))
        
    #         # sort noise for each sample
    #         ids_skeleton = self.weak_skeleton(data_skeleton)
    #         ids_restore = torch.argsort(ids_skeleton, dim=1)

    #         # keep the first subset
    #         ids_keep = ids_skeleton[:, :len_keep]
    #         x_masked = torch.gather(x, dim=1, index=ids_keep.unsqueeze(-1).repeat(1, 1, D))

    #         # generate the binary mask: 0 is keep, 1 is remove
    #         mask = torch.ones([N, L], device=x.device)
    #         mask[:, :len_keep] = 0
    #         # unshuffle to get the binary mask
    #         mask = torch.gather(mask, dim=1, index=ids_restore)

    #         return x_masked, mask, ids_restore
    def weak_skeleton(self,data_skeleton,random_ratio,dya_ratio,combine=True):
        B,C, T, V, M =data_skeleton.shape
        assert (C == 3)
        #score = data_skeleton[2, :, :, :].sum(axis=1)
    
        # data of frame 1
        xy1 = data_skeleton[:,0:2, 0:T - 1, :, :]#.reshape(2, T - 1, V, M, 1)
        # data of frame 2
        xy2 =data_skeleton[:,0:2, 1:T, :, :]#.reshape(2, T - 1, V, 1, M)
        # square of distance between frame 1&2 M:0
        distance = ((xy2 - xy1)**2).sum(axis=2).sum(axis=1)[...,0]
        # a weak point in a sequence of movements
        ids_skeleton=distance.argsort(descending=False).cuda()#默认从大到小排序
        if combine :
        
            mask_ratio=random_ratio+dya_ratio 

            len_keep = int(V * (1 - mask_ratio))

            #dyna_len_keep = int(len_keep * (1 - dya_ratio))
            dyna_len_keep=int(len_keep * (dya_ratio/mask_ratio))
            random_len_keep= len_keep-dyna_len_keep
            
            # ids of weak inactive ponits
            weak_ids=torch.arange(dyna_len_keep).reshape(1,dyna_len_keep).repeat(B, 1).cuda()
            
            #disrupting the remaining points
            random_ids=torch.stack([torch.arange(dyna_len_keep,V)[torch.randperm(V-dyna_len_keep)] for _ in range(B)]).cuda()

            dyna_random_ids=torch.cat((weak_ids,random_ids),dim=1).cuda()
            
            #print("原来第一个batch",ids_skeleton[0])
            #print("ids序号索引为",dyna_random_ids[0])
            ids_skeleton=torch.gather(ids_skeleton, dim=1, index=dyna_random_ids)  #ids_skeleton[dyna_random_ids]
            #print("改变后第一个batch",ids_skeleton[0])

        return ids_skeleton

    def dynamic_masking(self,x,data_skeleton,random_ratio,dya_ratio):
        """
        Perform per-sample random masking by per-sample shuffling.
        Per-sample shuffling is done by argsort random noise.
        x: [N, L, D], sequence
        """
        #imge 64 196 1024
        #     18
        mask_ratio=random_ratio+dya_ratio 
        N, L, D = x.shape  # batch, length, dim
        len_keep = int(L * (1 - mask_ratio))
       
        # sort noise for each sample
        ids_skeleton = self.weak_skeleton(data_skeleton,random_ratio,dya_ratio,True)
        ids_restore = torch.argsort(ids_skeleton, dim=1)

        # keep the first subset
        ids_keep = ids_skeleton[:, :len_keep]
        x_masked = torch.gather(x.cuda(), dim=1, index=ids_keep.unsqueeze(-1).repeat(1, 1, D))

        # generate the binary mask: 0 is keep, 1 is remove
        mask = torch.ones([N, L], device=x.device)
        mask[:, :len_keep] = 0
        # unshuffle to get the binary mask
        mask = torch.gather(mask.cuda(), dim=1, index=ids_restore)

        return x_masked, mask, ids_restore
    # def skeleton_ids(self,data_skeleton,dynamic_ratio,static_ratio,combine=True):
    #     B,C, T, V, M =data_skeleton.shape
    #     assert (C == 3)
    #     #score = data_skeleton[2, :, :, :].sum(axis=1)
    
    #     # data of frame 1
    #     xy1 = data_skeleton[:,0:2, 0:T - 1, :, :]#.reshape(2, T - 1, V, M, 1)
    #     # data of frame 2
    #     xy2 =data_skeleton[:,0:2, 1:T, :, :]#.reshape(2, T - 1, V, 1, M)
    #     # square of distance between frame 1&2 M:0
    #     distance = ((xy2 - xy1)**2).sum(axis=2).sum(axis=1)[...,0]
    #     # a weak point in a sequence of movements
    #     ids_skeleton=distance.argsort(descending=False).cuda()#默认从大到小排序
    #     if combine :
        
    #         mask_ratio=dynamic_ratio+static_ratio
            
    #         len_keep = int(V * (1 - mask_ratio))
    #         dyna_len_keep=int(len_keep * (dynamic_ratio/mask_ratio))
    #         static_len_keep = len_keep-dyna_len_keep#int(len_keep * (dya_ratio/mask_ratio))
    #         split_len=int(V/2)
    #         static_ids=torch.stack([torch.arange(0,split_len)[torch.randperm(split_len)] for _ in range(B)]).cuda()
        
    #         dynamic_ids=torch.stack([torch.arange(split_len,V)[torch.randperm(V-split_len)] for _ in range(B)]).cuda()

    #         saved_ids=torch.cat((static_ids[:,:static_len_keep],dynamic_ids[:,:dyna_len_keep]),dim=1).cuda()

    #         masked_ids=torch.cat((static_ids[:,static_len_keep:],dynamic_ids[:,dyna_len_keep:]),dim=1).cuda()
            
    #         saved_masked_ids=torch.cat((saved_ids,masked_ids),dim=1).cuda()

    #         ids_skeleton=torch.gather(ids_skeleton, dim=1, index=saved_masked_ids)


    #     return ids_skeleton
    def random_static_dynamic(self,ids_skeleton,dynamic_ratio,static_ratio,L,B):
          
            mask_ratio=static_ratio+dynamic_ratio
        
            len_keep = int(L * (1 - mask_ratio))

            static_len_keep=int(len_keep * (static_ratio/mask_ratio))

            dyna_len_keep = len_keep-static_len_keep#int(len_keep * (dya_ratio/mask_ratio))

            split_len=int(L/2)
        
            static_ids=torch.stack([torch.arange(0,split_len)[torch.randperm(split_len)] for _ in range(B)]).cuda()
        
            dynamic_ids=torch.stack([torch.arange(split_len,L)[torch.randperm(L-split_len)] for _ in range(B)]).cuda()

            saved_ids=torch.cat((static_ids[:,:static_len_keep],dynamic_ids[:,:dyna_len_keep]),dim=1).cuda()

            masked_ids=torch.cat((static_ids[:,static_len_keep:],dynamic_ids[:,dyna_len_keep:]),dim=1).cuda()
            
            saved_masked_ids=torch.cat((saved_ids,masked_ids),dim=1).cuda()

            ids_skeleton=torch.gather(ids_skeleton, dim=1, index=saved_masked_ids)  #ids_skeleton[dyna_random_ids]

            return ids_skeleton
    def skeleton_ids(self,data_skeleton,dynamic_ratio,static_ratio,L):
        B,T,V,C =data_skeleton.shape  ## (64,3,120,18,2)  ### 划分num——patches
        assert (C == 256)
        x_motion=torch.zeros_like(data_skeleton)  ## 64 30 18 256       建立全为0张量储存相应的运动信息  取绝对值 计算运动高低 由大到小排列
        x_motion=data_skeleton[:, 1:, :, :] -data_skeleton[:, :-1, :, :]  #28
        x_motion=torch.abs(x_motion) ## 绝对值  来看大小
        # data_skeleton[:,0:1,:,:]=0
        x_motion[:,0:1,:,:]=0  ##0时刻运动流为0
        # x_motion
        x_motion=torch.cat([x_motion[:,0:1,:,:],x_motion],dim=1) #64 30 18 256
        x_motion=x_motion.mean(dim=[3]) ## 进行维度转换
        x_motion=x_motion.reshape(B,T*V)  ### 
        # ids_restore=x_motion.argsort(descending=False).cuda() ## 由大到小排列    64  540 

        # x_motion=x_motion  ### 如何加入噪声...  归一化. (0,1)
        x_motion = x_motion / (torch.max(x_motion, dim=-1, keepdim=True).values * 0.8 + 1e-10)
        x_orig_motion_prob = F.softmax(x_motion, dim=-1)
        ##64 540
        
        noise = torch.log(x_orig_motion_prob) - torch.log(-torch.log(torch.rand(B, L, device=data_skeleton.device) + 1e-10) + 1e-10) 
        ### 静态、动态比例
        ids_restore=noise.argsort(descending=False).cuda()
        ids=self.random_static_dynamic(ids_restore,static_ratio,dynamic_ratio,L,B)
        # x_motion=x_motion.reshape(B,T,-1)
        # x_motion=torch.cat()
        # xy1 = data_skeleton[:,0:2, 0:T - 1, :, :][...,0]#.reshape(2, T - 1, V, M, 1)
        # #(64,2,119,18)    # data of frame 2
        # xy2 =data_skeleton[:,0:2, 1:T, :, :][...,0]#.reshape(2, T - 1, V, 1, M)
        # patch_temporal=T%self.patch_embed.patch_size[1]
        # assert (patch_temporal ==0)
        # ids=torch.zeros([B,int(T/self.patch_embed.patch_size[1]),V])
        # for t in range(0,int(T/self.patch_embed.patch_size[1])):
        #   ibegin=t*15
        #   iend=(t+1)*15
        #   xy_sub2=xy2[:,:,ibegin:iend,:]
        #   xy_sub1=xy1[:,:,ibegin:iend,:]
        #   d_i=((xy_sub2 - xy_sub1)**2).sum(axis=2).sum(axis=1)
        #   ids[:,t,]=d_i
        # ids=ids.transpose(2,1).flatten(1)
        # #new_ids=ids.flatten(1)
        # d_sorted=ids.argsort(descending=False).cuda()
        #19*
        return ids,noise
    def motion_aware_masking(self,x,data_skeleton,dynamic_ratio,static_ratio,kept_mask_ratio):
        """
        Perform per-sample random masking by per-sample shuffling.
        Per-sample shuffling is done by argsort random noise.
        x: [N, L, D], sequence
        """
        #imge 64 196 1024
        #     18
        mask_ratio=dynamic_ratio+static_ratio
        N, L, D = x.shape  # batch, length, dim    64    540  256
        len_keep = int(L * (1 - mask_ratio)) ### 可以再从masktoken中 再选取partial进行复原？
        len_masked=int(L*(mask_ratio-kept_mask_ratio))
        # _, TP, VP, _ = x_orig.shape
        # len_masked = int(L * (mask_ratio - kept_mask_ratio))
        # sort noise for each sample
        ids_skeleton,noise = self.skeleton_ids(data_skeleton,dynamic_ratio,static_ratio,L) ## 噪声和mae实现方法一样
        ids_shuffle = torch.argsort(ids_skeleton, dim=1) 

        ids_restore = torch.argsort(ids_shuffle, dim=1)
        # keep the first subset、

        # ids_keep = ids_skeleton[:, :len_keep]
        ids_keep = ids_shuffle[:, :len_keep]
        x_masked = torch.gather(x.cuda(), dim=1, index=ids_keep.unsqueeze(-1).repeat(1, 1, D))
        # generate the binary mask: 0 is keep, 1 is remove
        mask = torch.ones([N, L], device=x.device)
        # mask[:, :len_keep] = 0
        if kept_mask_ratio>0:

            mask[:, :(len_keep+len_masked)] = 0
        else:
            mask[:, :(len_keep)] = 0
        # unshuffle to get the binary mask
        mask = torch.gather(mask.cuda(), dim=1, index=ids_restore)
        
        return x_masked, mask, ids_restore
    def forward_encoder(self, x, mask_ratio,kept_mask_ratio):
        #data.shape N,C,T,V,M
        # N, C, T, V, M=x.size()
        #image 64 3 224 224
        # embed patches out b 14*14 1024   224/16
        #sklearn b 18 576
        # print("原来的形状，：",x.shape)    (64,3,120,18,2)
        #128*(270)*512 (64,3,120,18)->(64,120,18,3)
        # x=x.reshape(N*M,C,T,V).permute(0,2,3,1) 
        
        # x_copy
        x = self.joints_embed(x)
        NM, TP, VP, _ = x.shape ## 64 120 18 256  64 30 18 256
        x_copy=x.clone()
        ### 时间维度和空间维度嵌入   
        x = x + self.pos_embed[:, :, :VP, :] + self.temp_embed[:, :TP, :, :]
        #64 270 512
        # add pos embed w/o cls token
        # x = x + self.pos_embed[:, 1:, :]
        x = x.reshape(NM, TP * VP, -1)
        coords = None
        # x, mask, ids_restore, _ = self.random_masking(x, mask_ratio)
        # masking: length -> length * mask_ratio  
        #self.uniform_masking(x,mask_ratio)
        #x b 64 512
        # x, mask, ids_restore =self.motion_aware_masking(x, x_copy,mask_ratio-0.5,0.5,kept_mask_ratio)#self.u        x, mask, ids_restore=self.dynamic_masking(x, x_copy,0.35,mask_ratio-0.35)niform_masking(x,mask_ratio)#self.random_masking(x, mask_ratio)#self.motion_aware_masking(x, x_copy,mask_ratio-0.5,0.5)#self.random_masking(x, mask_ratio)#self.motion_aware_masking(x, x_copy,mask_ratio-0.5,0.5)#self.random_masking(x, mask_ratio)#self.motion_aware_masking(x, x_copy,mask_ratio-0.25,0.25)#self.uniform_masking(x,mask_ratio)#self.random_masking(x, mask_ratio)##self.motion_aware_masking(x, x_copy,mask_ratio-0.25,0.25)#self.random_masking(x, mask_ratio)#self.motion_aware_masking(x, x_copy,mask_ratio-0.25,0.25)#self.random_masking(x, mask_ratio)#self.dynamic_masking(x, x_copy,0.25,mask_ratio-0.25)##
        x, mask, ids_restore=self.motion_aware_masking(x, x_copy, mask_ratio - 0.5, 0.5,kept_mask_ratio)
        cls_tokens = self.cls_token.expand(x.shape[0], -1, -1)
        x = torch.cat((cls_tokens, x), dim=1)
        # self.dynamic_masking(x, x_copy,0.35,mask_ratio-0.35) #self.uniform_masking(x,mask_ratio)
        #x 64 53 512
        # append cls token
        # cls_token = self.cls_token + self.pos_embed[:, :1, :]
        # #imge 64 1 1024
        # #sk   64 1  768
        # cls_tokens = cls_token.expand(x.shape[0], -1, -1)
        # #64 1351 768
        # x = torch.cat((cls_tokens.cuda(), x), dim=1)

        # apply Transformer blocks
        for blk in self.blocks:

            x = blk(x)
        x = self.norm(x)

        return x, mask, ids_restore,coords

    def forward_decoder(self, y,mask, ids_restore):
        # embed tokens
        NM = y.shape[0]
        T = self.joints_embed.t_grid_size
        V= self.joints_embed.grid_size
        x = self.decoder_embed(y)
        C = x.shape[-1]
        # append mask tokens to sequence
        # mask_tokens = self.mask_token.repeat(x.shape[0], ids_restore.shape[1] + 1 - x.shape[1], 1)
        # x_ = torch.cat([x[:, 1:, :], mask_tokens], dim=1)  # no cls token
        # x_ = torch.gather(x_, dim=1, index=ids_restore.unsqueeze(-1).repeat(1, 1, x.shape[2]))  # unshuffle
        # x = torch.cat([x[:, :1, :], x_], dim=1)  # append cls token
        mask_tokens = self.mask_token.repeat(NM, T * V - x.shape[1], 1)
        x_ = torch.cat([x[:, :, :], mask_tokens], dim=1)  # no cls token
        x_ = torch.gather(
            x_, dim=1, index=ids_restore.unsqueeze(-1).repeat(1, 1, x_.shape[2])
        )  # unshuffle
        x = x_.view([NM, T, V, C])

        # add pos & temp embed
        x = x + self.decoder_pos_embed[:, :, :V, :] + self.decoder_temp_embed[:, :T, :, :]  # NM, TP, VP, C
        # apply Transformer blocks
        # add pos embed
        # N,L=ids_restore.shape
        # x = x.reshape(NM, T * V, C)
        # x = self.decoder_pos_embed[:, 1:].masked_select(mask.bool().unsqueeze(-1)).reshape(N, -1, self.mask_token.shape[-1])
        # x = x + self.mask_token
        # x = x + self.decoder_pos_embed
        #2261*8
        # apply Transformer blocks
        #
        # x = x_.view([NM, TP, VP, C])
        # x = x + self.decoder_pos_embed[:, :, :VP, :] + self.decoder_temp_embed[:, :TP, :, :]
        x = x.reshape(NM, T * V, C)

        for blk in self.decoder_blocks:
            x = blk(x, y)
        # predictor projection
        x = self.decoder_norm(x)
        x = self.decoder_pred(x)

        # remove cls token
        x = x[:, 1:, :]

        return x

    def forward_loss(self, imgs, pred, mask):
        """
        imgs: [N, 3, H, W] 64 3 300 18 2
        pred: [N, L, p*p*3]
        mask: [N, L], 0 is keep, 1 is remove,
        """
        #img 14*14 16*16*3
        #skeleton 19*140  3
        target = self.patchify(imgs)
        target = target.masked_select(mask.bool().unsqueeze(-1)).reshape(target.shape[0], -1, target.shape[-1])
        if self.norm_pix_loss:
            mean = target.mean(dim=-1, keepdim=True)
            var = target.var(dim=-1, keepdim=True)
            target = (target - mean) / (var + 1.e-6)**.5

        loss = (pred - target) ** 2
        loss = loss.mean(dim=-1)  # [N, L], mean loss per patch
        #
        loss = (loss * mask).sum() / mask.sum()  # mean loss on removed patches
        return loss

    def forward(self, x, mask_ratio,kept_mask_ratio):
        #img:->skeleton 64 3 300 18
        N, C, T, V, M = x.shape# （32,3,120,18,2）
        x = x.permute(0, 4, 2, 3, 1).contiguous().view(N * M, T, V, C)
        # x=x[...,0] #32 3  120 18
        # x = x.permute(0, 2, 3, 1).contiguous().view(N, T, V, C)
        # # x=x[...,0] #(32,3,120,18)
        latent, mask, ids_restore,coords = self.forward_encoder(x, mask_ratio,kept_mask_ratio)  ##   64  120  18 3
        #64 18 600
        pred= self.forward_decoder(latent,mask, ids_restore)  # [N, L, p*p*3]
        loss = self.forward_loss(x, pred, mask)
        return loss, pred, mask

#512 256
def mae_vit_base_patch16_dec512d8b(**kwargs):
    model = MaskedAutoencoderViT(
          frame_nums=120,skeleton_nums=18,patch_size=1, t_patch_size=4, embed_dim=256, depth=8, num_heads=8,
        decoder_embed_dim=256, decoder_depth=6, decoder_num_heads=8,
        mlp_ratio=4, norm_layer=partial(nn.LayerNorm, eps=1e-6), **kwargs)
    return model


def mae_vit_large_patch16_dec512d8b(**kwargs):
    model = MaskedAutoencoderViT(
        skeleton_nums=19,patch_size=20, embed_dim=512, depth=8, num_heads=8,
        decoder_embed_dim=256, decoder_depth=6, decoder_num_heads=8,
        mlp_ratio=4, norm_layer=partial(nn.LayerNorm, eps=1e-6), **kwargs)
    return model


# def mae_vit_huge_patch14_dec512d8b(**kwargs):
#     model = MaskedAutoencoderViT(
#         patch_size=14, embed_dim=1280, depth=32, num_heads=16,
#         decoder_embed_dim=512, decoder_depth=8, decoder_num_heads=16,
#         mlp_ratio=4, norm_layer=partial(nn.LayerNorm, eps=1e-6), **kwargs)
#     return model


# set recommended archs
mae_vit_base_patch16 = mae_vit_base_patch16_dec512d8b  # decoder: 512 dim, 8 blocks
mae_vit_large_patch16 = mae_vit_large_patch16_dec512d8b  # decoder: 512 dim, 8 blocks

