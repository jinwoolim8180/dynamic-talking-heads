""" Implementation of the three networks that make up the Talking Heads generative model. """
from collections import OrderedDict

import torch
import torch.nn as nn
from torch.nn import functional as F

from .components import ResidualBlock, ResidualBlockUp, ResidualBlockDown, AdaptiveResidualBlockUp, SelfAttention
import config


def weights_init(m):
    classname = m.__class__.__name__
    if classname.find('Conv2d') != -1:
        m.weight.data.normal_(0.0, 0.02)
    if classname.find('Linear') != -1:
        # m.weight.data.normal_(0.0, 0.02)
        nn.init.xavier_uniform_(m.weight.data)
        m.bias.data.fill_(0)
    elif classname.find('InstanceNorm2d') != -1:
        m.weight.data.normal_(1.0, 0.02)
        m.bias.data.fill_(0)


class MotionEncoder(nn.Module):
    """
    Motion Encoder network extracts features related to overall motions of a head.
    Features are decomposed into static ones and dynamic ones.
    """
    def __init__(self, gpu=None):
        super(MotionEncoder, self).__init__()

        # encoding layers
        self.conv1 = ResidualBlockDown(3, 128)
        self.in1_e = nn.InstanceNorm2d(128, affine=True)

        self.conv2 = ResidualBlockDown(128, 256)
        self.in2_e = nn.InstanceNorm2d(256, affine=True)

        self.conv3 = ResidualBlockDown(256, 512)
        self.in3_e = nn.InstanceNorm2d(512, affine=True)

        self.conv4 = ResidualBlockDown(512, 512)
        self.in4_e = nn.InstanceNorm2d(512, affine=True)

        self.pooling = nn.AdaptiveMaxPool2d((1, 1))

        self.gru = nn.GRU(512, 512, batch_first=True)
        self.fc = nn.Linear(512, 128)

    def forward(self, x):
        B, K, C, H, W = x.shape
        x = x.view(-1, C, H, W)

        # Encode
        out = self.conv1(x)
        out = self.in1_e(x)
        out = self.conv2(x)
        out = self.in2_e(x)
        out = self.conv3(x)
        out = self.in3_e(x)
        out = self.conv4(x)
        out = self.in4_e(x)
        out = F.relu(self.pooling(out))

        x = x.view(B, K, -1)
        early = self.fc(x)
        static = self.gru(out)
        return static, early


class DynamicAdder(nn.Module):
    """
    Dynamic Adder network receives a 'dynamic' vector from Motion Encoder and upsamples it.
    """
    def __init__(self, gpu=None):
        super(DynamicAdder, self).__init__(self)

        # Decoder
        self.fc = nn.Linear(1, 16)

        self.deconv2 = ResidualBlockUp(128, 128, upsample=4)
        self.in2_d = nn.InstanceNorm2d(128, affine=True)

        self.deconv1 = ResidualBlockUp(128, 128, upsample=4)
        self.in1_d = nn.InstanceNorm2d(128, affine=True)

    def forward(self, x):
        B, _ = x.shape
        out = self.fc(x.unsqueeze(2)).view(B, 4, 4)
        out = self.in2_d(self.deconv2(out))
        out = self.in1_d(self.deconv1(out))
        return out


class Embedder(nn.Module):
    """
    The Embedder network attempts to generate a vector that encodes the personal characteristics of an individual given
    a head-shot and the matching landmarks.
    """
    def __init__(self, gpu=None):
        super(Embedder, self).__init__()

        self.conv1 = ResidualBlockDown(3, 64)
        self.conv2 = ResidualBlockDown(64, 128)
        self.conv3 = ResidualBlockDown(128, 256)
        self.att = SelfAttention(256)
        self.conv4 = ResidualBlockDown(256, 512)
        self.conv5 = ResidualBlockDown(512, 512)
        self.conv6 = ResidualBlockDown(512, 512)

        self.pooling = nn.AdaptiveMaxPool2d((1, 1))

        self.apply(weights_init)
        self.gpu = gpu
        if gpu is not None:
            self.cuda(gpu)

    def forward(self, x):
        assert x.dim() == 4 and x.shape[1] == 3, "Both x and y must be tensors with shape [BxK, 3, W, H]."
        if self.gpu is not None:
            x = x.cuda(self.gpu)

        # Encode
        out = (self.conv1(x))  # [BxK, 64, 128, 128]
        out = (self.conv2(out))  # [BxK, 128, 64, 64]
        out = (self.conv3(out))  # [BxK, 256, 32, 32]
        out = self.att(out)
        out = (self.conv4(out))  # [BxK, 512, 16, 16]
        out = (self.conv5(out))  # [BxK, 512, 8, 8]
        out = (self.conv6(out))  # [BxK, 512, 4, 4]

        # Vectorize
        out = F.relu(self.pooling(out).view(-1, config.E_VECTOR_LENGTH))

        return out


class Generator(nn.Module):
    ADAIN_LAYERS = OrderedDict([
        ('deconv6', (512, 512)),
        ('deconv5', (512, 512)),
        ('deconv4', (512, 256)),
        ('deconv3', (256, 128)),
        ('deconv2', (128, 64)),
        ('deconv1', (64, 3))
    ])

    def __init__(self, gpu=None):
        super(Generator, self).__init__()

        # projection layer
        self.PSI_PORTIONS, self.psi_length = self.define_psi_slices()
        self.projection = nn.Parameter(torch.rand(self.psi_length, config.E_VECTOR_LENGTH).normal_(0.0, 0.02))
        self.static_fc = nn.Parameter(512, 512)

        # decoding layers
        self.fc = nn.Linear(1, 16)

        self.deconv6 = AdaptiveResidualBlockUp(512, 512, upsample=2)
        self.in6_d = nn.InstanceNorm2d(512, affine=True)

        self.deconv5 = AdaptiveResidualBlockUp(512, 512, upsample=2)
        self.in5_d = nn.InstanceNorm2d(512, affine=True)

        self.deconv4 = AdaptiveResidualBlockUp(512, 256, upsample=2)
        self.in4_d = nn.InstanceNorm2d(256, affine=True)

        self.deconv3 = AdaptiveResidualBlockUp(256, 128, upsample=2)
        self.in3_d = nn.InstanceNorm2d(128, affine=True)

        self.att2 = SelfAttention(128)

        self.deconv2 = AdaptiveResidualBlockUp(128, 64, upsample=2)
        self.in2_d = nn.InstanceNorm2d(64, affine=True)

        self.deconv1 = AdaptiveResidualBlockUp(64, 3, upsample=2)
        self.in1_d = nn.InstanceNorm2d(3, affine=True)

        self.apply(weights_init)
        self.gpu = gpu
        if gpu is not None:
            self.cuda(gpu)

    def forward(self, y, e, s=None, d=None):
        if self.gpu is not None:
            e = e.cuda(self.gpu)
            y = y.cuda(self.gpu)

        B, _, H, W = y.shape
        out = y  # [B, 512]
        if s is not None:
            y += self.static_fc(s)

        # Calculate psi_hat parameters
        P = self.projection.unsqueeze(0)
        P = P.expand(e.shape[0], P.shape[1], P.shape[2])
        psi_hat = torch.bmm(P, e.unsqueeze(2)).squeeze(2)

        # Decode
        out = self.fc(out.unsqueeze(2)).view(B, -1, 4, 4)
        out = self.in6_d(self.deconv6(out, *self.slice_psi(psi_hat, 'deconv6')))  # [B, 512, 4, 4]
        out = self.in5_d(self.deconv5(out, *self.slice_psi(psi_hat, 'deconv5')))  # [B, 512, 16, 16]
        out = self.in4_d(self.deconv4(out, *self.slice_psi(psi_hat, 'deconv4')))  # [B, 256, 32, 32]
        out = self.in3_d(self.deconv3(out, *self.slice_psi(psi_hat, 'deconv3')))  # [B, 128, 64, 64]
        if d is not None:
            out += d
        out = self.att2(out)
        out = self.in2_d(self.deconv2(out, *self.slice_psi(psi_hat, 'deconv2')))  # [B, 64, 128, 128]
        out = self.in1_d(self.deconv1(out, *self.slice_psi(psi_hat, 'deconv1')))  # [B, 3, 256, 256]

        out = torch.sigmoid(out)

        return out

    def slice_psi(self, psi, portion):
        idx0, idx1 = self.PSI_PORTIONS[portion]
        len1, len2 = self.ADAIN_LAYERS[portion]
        aux = psi[:, idx0:idx1].unsqueeze(-1)
        mean1, std1 = aux[:, 0:len1], aux[:, len1:2 * len1]
        mean2, std2 = aux[:, 2 * len1:2 * len1 + len2], aux[:, 2 * len1 + len2:]
        return mean1, std1, mean2, std2

    def define_psi_slices(self):
        out = {}
        d = self.ADAIN_LAYERS
        start_idx, end_idx = 0, 0
        for layer in d:
            end_idx = start_idx + d[layer][0] * 2 + d[layer][1] * 2
            out[layer] = (start_idx, end_idx)
            start_idx = end_idx

        return out, end_idx


class Discriminator(nn.Module):
    def __init__(self, training_videos, gpu=None):
        super(Discriminator, self).__init__()

        self.conv1 = ResidualBlockDown(6, 64)
        self.conv2 = ResidualBlockDown(64, 128)
        self.conv3 = ResidualBlockDown(128, 256)
        self.att = SelfAttention(256)
        self.conv4 = ResidualBlockDown(256, 512)
        self.conv5 = ResidualBlockDown(512, 512)
        self.conv6 = ResidualBlockDown(512, 512)
        self.res_block = ResidualBlock(512)

        self.pooling = nn.AdaptiveMaxPool2d((1, 1))

        self.W = nn.Parameter(torch.rand(512, training_videos).normal_(0.0, 0.02))
        self.w_0 = nn.Parameter(torch.rand(512, 1).normal_(0.0, 0.02))
        self.b = nn.Parameter(torch.rand(1).normal_(0.0, 0.02))

        self.apply(weights_init)
        self.gpu = gpu
        if gpu is not None:
            self.cuda(gpu)

    def forward(self, x, y, i):
        assert x.dim() == 4 and x.shape[1] == 3, "Both x and y must be tensors with shape [BxK, 3, W, H]."
        assert x.shape == y.shape, "Both x and y must be tensors with shape [BxK, 3, W, H]."

        if self.gpu is not None:
            x = x.cuda(self.gpu)
            y = y.cuda(self.gpu)

        # Concatenate x & y
        out = torch.cat((x, y), dim=1)  # [B, 6, 256, 256]

        # Encode
        out_0 = (self.conv1(out))  # [B, 64, 128, 128]
        out_1 = (self.conv2(out_0))  # [B, 128, 64, 64]
        out_2 = (self.conv3(out_1))  # [B, 256, 32, 32]
        out_3 = self.att(out_2)
        out_4 = (self.conv4(out_3))  # [B, 512, 16, 16]
        out_5 = (self.conv5(out_4))  # [B, 512, 8, 8]
        out_6 = (self.conv6(out_5))  # [B, 512, 4, 4]
        out_7 = (self.res_block(out_6))

        # Vectorize
        out = F.relu(self.pooling(out_7)).view(-1, 512, 1)  # [B, 512, 1]

        # Calculate Realism Score
        _out = out.transpose(1, 2)
        _W_i = (self.W[:, i].unsqueeze(-1)).transpose(0, 1)
        out = torch.bmm(_out, _W_i + self.w_0) + self.b
        out = torch.sigmoid(out)

        out = out.reshape(x.shape[0])

        return out, [out_0, out_1, out_2, out_3, out_4, out_5, out_6, out_7]
