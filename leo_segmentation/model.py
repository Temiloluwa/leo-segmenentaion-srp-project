import os
import torch
import gc
import numpy as np
from torch import nn
from torch.distributions import Normal
from torch.nn import CrossEntropyLoss
from torchvision import models
from torch.nn import functional as F

from leo_segmentation.utils import display_data_shape, get_named_dict, calc_iou_per_class, \
    log_data, load_config, list_to_tensor, numpy_to_tensor, tensor_to_numpy, update_config
from leo_segmentation.data import PascalDatagenerator, GeneralDatagenerator, TrainingStats

config = load_config()
hyp = config.hyperparameters
device = torch.device("cuda:0" if torch.cuda.is_available()
                                  and config.use_gpu else "cpu")


class EncoderBlock(nn.Module):
    """ Encoder with pretrained backbone """

    def __init__(self):
        super(EncoderBlock, self).__init__()
        self.layers = nn.ModuleList(list(models.mobilenet_v2(pretrained=True)
                                         .features))
        self.squeeze_conv_l1 = nn.Conv2d(in_channels=16, out_channels=1, kernel_size=(1, 1), padding=(0, 0), stride=1)
        self.squeeze_conv_l3 = nn.Conv2d(in_channels=24, out_channels=1, kernel_size=(1, 1), padding=(0, 0), stride=1)
        self.squeeze_conv_l6 = nn.Conv2d(in_channels=32, out_channels=1, kernel_size=(1, 1), padding=(0, 0), stride=1)
        self.squeeze_conv_l13 = nn.Conv2d(in_channels=96, out_channels=1, kernel_size=(1, 1), padding=(0, 0), stride=1)
        self.squeeze_conv_l17 = nn.Conv2d(in_channels=320, out_channels=1, kernel_size=(1, 1), padding=(0, 0), stride=1)
        self.sigmoid = nn.Sigmoid()

    def forward(self, x, d_train=False, we=None):
        features = []
        cnt = 0
        we = [] if we is None else we
        output_layers = [1, 3, 6, 13]  # 112, 56, 28, 14
        for i, layer in enumerate(self.layers):
            x = layer(x)
            if i in output_layers:
                if d_train == True:
                    features.append(x)
                    squeeze_conv_layer = getattr(self, f"squeeze_conv_l{i}")
                    we.append(self.sigmoid(squeeze_conv_layer(x)))
                elif len(we) > 0:
                    x = torch.mul(x, we[cnt])
                    features.append(x)
                    cnt += 1
                else:
                    features.append(x)
        latents = x
        if d_train == True:
            return features, latents, we
        return features, latents


class AttentionNetwork(nn.Module):
    def __init__(self, in_channels_skip, in_channels_pre_layer_out, out_channels):
        super(AttentionNetwork, self).__init__()
        self.skip_layer = nn.Sequential(nn.Conv2d(in_channels_skip, out_channels, kernel_size=1),
                                        nn.BatchNorm2d(out_channels))
        self.pre_layer = nn.Sequential(nn.Conv2d(in_channels_pre_layer_out, out_channels, kernel_size=1),
                                       nn.BatchNorm2d(out_channels))
        self.psi = nn.Sequential(nn.Conv2d(out_channels, 1, kernel_size=1),
                                 nn.BatchNorm2d(1),
                                 nn.Sigmoid())

    def forward(self, skip, pre_layer_out):
        skip1 = self.skip_layer(skip)
        pre_layer1 = nn.functional.interpolate(self.pre_layer(pre_layer_out), skip1.shape[2:], mode='bilinear',
                                               align_corners=False)
        out = self.psi(nn.ReLU()(skip1 + pre_layer1))
        out = nn.Sigmoid()(out)
        return out * skip

def decoder_block(conv_in_size, conv_out_size):
    """ Sequentical group formimg a decoder block """
    layers = [

        nn.Conv2d(conv_in_size, conv_out_size,
                  kernel_size=3, stride=1, padding=1),
        nn.ReLU(),
        nn.Dropout(hyp.dropout_rate),
        nn.BatchNorm2d(conv_out_size),
        nn.Conv2d(conv_out_size, conv_out_size,
                  kernel_size=3, stride=1, padding=1),
        nn.ReLU(),
        nn.ConvTranspose2d(conv_out_size, conv_out_size,
                           kernel_size=4, stride=2, padding=1)
    ]
    conv_block = nn.Sequential(*layers)
    return conv_block


class DecoderBlock(nn.Module):
    """
    Leo Decoder
    """

    def __init__(self, skip_features, latents):
        super(DecoderBlock, self).__init__()
        self.upsample = nn.Upsample(scale_factor=2, mode='bilinear', align_corners=True)
        self.conv1 = decoder_block(latents.shape[1],
                                   hyp.base_num_covs * 4)
        self.conv2 = decoder_block(skip_features[-1].shape[1] + hyp.base_num_covs * 4,
                                   hyp.base_num_covs * 3)
        self.conv3 = decoder_block(skip_features[-2].shape[1] + hyp.base_num_covs * 3,
                                   hyp.base_num_covs * 2)
        self.conv4 = decoder_block(skip_features[-3].shape[1] + hyp.base_num_covs * 2,
                                   hyp.base_num_covs * 1)
        self.up_final = nn.ConvTranspose2d(skip_features[-4].shape[1] + hyp.base_num_covs * 1,
                                           hyp.base_num_covs, kernel_size=4, stride=2, padding=1)

        self.squeeze_conv_latent = nn.Conv2d(in_channels=1280, out_channels=1, kernel_size=(1, 1), padding=(0, 0),
                                             stride=1)
        self.squeeze_conv1_out = nn.Conv2d(in_channels=skip_features[-1].shape[1] + hyp.base_num_covs * 4, \
                                           out_channels=1, kernel_size=(1, 1), padding=(0, 0), stride=1)
        self.squeeze_conv2_out = nn.Conv2d(in_channels=skip_features[-2].shape[1] + hyp.base_num_covs * 3, \
                                           out_channels=1, kernel_size=(1, 1), padding=(0, 0), stride=1)
        self.squeeze_conv3_out = nn.Conv2d(in_channels=skip_features[-3].shape[1] + hyp.base_num_covs * 2, \
                                           out_channels=1, kernel_size=(1, 1), padding=(0, 0), stride=1)
        self.squeeze_conv4_out = nn.Conv2d(in_channels=skip_features[-4].shape[1] + hyp.base_num_covs, \
                                           out_channels=1, kernel_size=(1, 1), padding=(0, 0), stride=1)
        self.squeeze_final_out = nn.Conv2d(in_channels=hyp.base_num_covs, out_channels=1, kernel_size=(1, 1), \
                                           padding=(0, 0), stride=1)

        self.attention1 = AttentionNetwork(skip_features[-1].shape[1], hyp.base_num_covs * 4,
                                           int(skip_features[-1].shape[1] / 2))
        self.attention2 = AttentionNetwork(skip_features[-2].shape[1], hyp.base_num_covs * 3,
                                           int(skip_features[-2].shape[1] / 2))
        self.attention3 = AttentionNetwork(skip_features[-3].shape[1], hyp.base_num_covs * 2,
                                           int(skip_features[-1].shape[1] / 2))
        self.attention4 = AttentionNetwork(skip_features[-4].shape[1], hyp.base_num_covs,
                                           int(skip_features[-1].shape[1] / 2))

        self.sigmoid = nn.Sigmoid()

    def forward(self, skip_features, latents, d_train=False, wd=None):
        def prep_and_forward(i, o, wd):
            if latents.shape[0] == 1:
                skip_f = skip_features[-i].clone().repeat(5, 1, 1, 1)
            else:
                skip_f = skip_features[-i]
            attention = getattr(self, f"attention{i}")
            x = nn.functional.interpolate(o, skip_f.shape[2:], mode='bilinear', align_corners=False)
            o = torch.cat([attention(skip_f, o), x], dim=1)
            if d_train == True:
                squeeze_conv_out = getattr(self, f"squeeze_conv{i}_out")
                wd.append(self.sigmoid(squeeze_conv_out(o)))
            elif len(wd) > 0:
                o = torch.mul(o, wd[i])
            return o, wd

        wd = [] if wd is None else wd
        if d_train == True:
            wd.append(self.sigmoid(self.squeeze_conv_latent(latents)))
        elif len(wd) > 0:
            if latents.shape[0] == 1:
                latents_ = latents.clone().repeat(5, 1, 1, 1)
                latents_ = torch.mul(latents_, wd[0])
            else:
                latents = torch.mul(latents, wd[0])

        if latents.shape[0] == 1:
            o = self.conv1(latents_)
        else:
            o = self.conv1(latents)

        o, wd = prep_and_forward(1, o, wd)
        o = self.conv2(o)
        o, wd = prep_and_forward(2, o, wd)
        o = self.conv3(o)
        o, wd = prep_and_forward(3, o, wd)
        o = self.conv4(o)
        o, wd = prep_and_forward(4, o, wd)

        o = self.up_final(o)
        if latents.shape[0] == 1:
            o = torch.mean(o, dim=0).unsqueeze(0)
        if d_train == True:
            wd.append(self.sigmoid(self.squeeze_final_out(o)))
        elif len(wd) > 0:
            latents = torch.mul(o, wd[5])
        if d_train == True:
            return o, wd
        return o


class LEO(nn.Module):
    """
    contains functions to perform latent embedding optimization
    """

    def __init__(self, mode="meta_train"):
        super(LEO, self).__init__()
        self.mode = mode
        self.encoder = EncoderBlock()
        # self.aspp = build_aspp('mobilenet', 16, nn.BatchNorm2d)
        # self.RelationNetwork = RelationNetwork(512, 256)
        self.seg_network = nn.Conv2d(hyp.base_num_covs + 3, 2, kernel_size=3, stride=1, padding=1)
        self.seg_weight = self.seg_network.weight.detach().to(device)
        self.seg_weight.requires_grad = True

        self.loss_fn = CrossEntropyLoss()
        self.optimizer_seg_network = torch.optim.Adam(
            [self.seg_weight], lr=hyp.outer_loop_lr)

    def freeze_encoder(self):
        """ Freeze encoder weights """
        for param in self.encoder.parameters():
            param.requires_grad = False

    def unfreeze_encoder(self):
        """ UnFreeze encoder weights """
        for param in self.encoder.parameters():
            param.requires_grad = True

    def forward_encoder(self, x, mode, d_train=False, we=None):
        """ Performs forward pass through the encoder """
        if d_train == True:
            skip_features, latents, we = self.encoder(x, d_train=d_train)
        else:
            skip_features, latents = self.encoder(x, we)

        if not latents.requires_grad:
            latents.requires_grad = True
        if d_train == True:
            return skip_features, latents, we
        return skip_features, latents

    def forward_decoder(self, skip_features, latents, d_train=False, wd=None):
        """Performs forward pass through the decoder"""
        if d_train == True:
            output = self.decoder(skip_features, latents, d_train=d_train)
        elif wd != None:
            output = self.decoder(skip_features, latents, wd=wd)
        else:
            output = self.decoder(skip_features, latents)
        return output

    def forward_segnetwork(self, decoder_out, x, weight):
        """  Receives features from the decoder
             Concats the features with input image
             Convolution layer acts on the concatenated input
            Args:
                decoder_out (torch.Tensor): decoder output features
                x (torch.Tensor): input images
                weight(tf.tensor): kernels for the segmentation network
            Returns:
                pred(tf.tensor): predicted logits
        """
        o = torch.cat([decoder_out, x], dim=1)
        pred = F.conv2d(o, weight, padding=1)
        return pred

    def forward(self, x, d_train=False, latents=None, weight=None, we=None, wd=None):
        """ Performs a forward pass through the entire network
            - The Autoencoder generates features using the inputs
            - Features are concatenated with the inputs
            - The concatenated features are segmented
            Args:
                x (torch.Tensor): input image
                latents(torch.Tensor): output of the bottleneck
            Returns:
                latents(torch.Tensor): output of the bottleneck
                features(torch.Tensor): output of the decoder
                pred(torch.Tensor): predicted logits
                weight(torch.Tensor): segmentation weights
        """

        if latents is None:
            if d_train == True:
                skip_features, latents, we = self.forward_encoder(x, self.mode, d_train=d_train)
            else:
                skip_features, latents = self.forward_encoder(x, self.mode, we=we)
            self.skip_features = skip_features
        else:
            skip_features = self.skip_features

        if weight is not None:
            seg_weight = weight
        else:
            seg_weight = self.seg_weight

        if d_train == True:
            features, wd = self.forward_decoder(skip_features, latents, d_train=d_train)
        elif wd is not None:
            features = self.forward_decoder(skip_features, latents, wd=wd)
        else:
            features = self.forward_decoder(skip_features, latents)
        pred = self.forward_segnetwork(features, x, seg_weight)

        if d_train == True and we is not None:
            return latents, features, pred, we
        elif d_train == True and wd is not None:
            return latents, features, pred, wd

        return latents, features, pred

    def leo_inner_loop(self, x, y):
        """
        Performs innerloop optimization
            - It updates the latents taking gradients wrt the training loss
            - It generates better features after the latents are updated
            Args:
                x(torch.Tensor): input training image
                y(torch.Tensor): input training mask
            Returns:
                seg_weight_grad(torch.Tensor): The last gradient of the
                    training loss wrt to the segmenation weights
                features(torch.Tensor): The last generated features
        """
        inner_lr = hyp.inner_loop_lr
        latents, _, pred, w_e = self.forward(x, d_train=True)
        tr_loss = self.loss_fn(pred, y.long())
        for _ in range(hyp.num_adaptation_steps):
            latents_grad = torch.autograd.grad(tr_loss, [latents], retain_graph=True, create_graph=False)[0]
            with torch.no_grad():
                latents -= inner_lr * latents_grad
            latents, features, pred, w_d = self.forward(x, latents=latents, d_train=True)
            tr_loss = self.loss_fn(pred, y.long())
        seg_weight_grad = torch.autograd.grad(tr_loss, [self.seg_weight], retain_graph=True, create_graph=False)[0]

        return seg_weight_grad, features, w_e, w_d

    def finetuning_inner_loop(self, data_dict, tr_features, seg_weight_grad, transformers, mode, we=None, wd=None):
        """ Finetunes the segmenation weights/kernels by performing MAML
            Args:
                data_dict (dict): contains tr_imgs, tr_masks, val_imgs, val_masks
                tr_features (torch.Tensor): tensor containing decoder features
                segmentation_grad (torch.Tensor): gradients of the training
                                                loss to the segmenation weights
            Returns:
                val_loss (torch.Tensor): validation loss
                seg_weight_grad (torch.Tensor): gradient of validation loss
                                                wrt segmentation weights
                decoder_grads (torch.Tensor): gradient of validation loss
                                                wrt decoder weights
                transformers(tuple): tuple of image and mask transformers
                weight (torch.Tensor): segmentation weights
        """
        img_transformer, mask_transformer = transformers
        weight = self.seg_weight - hyp.finetuning_lr * seg_weight_grad
        for _ in range(hyp.num_finetuning_steps - 1):
            pred = self.forward_segnetwork(tr_features, data_dict.tr_imgs, weight)
            tr_loss = self.loss_fn(pred, data_dict.tr_masks.long())
            seg_weight_grad = torch.autograd.grad(tr_loss, [weight], retain_graph=True, create_graph=False)[0]
            weight -= hyp.finetuning_lr * seg_weight_grad

        if mode == "meta_train":
            _, _, prediction = self.forward(data_dict.val_imgs, weight=weight, we=we, wd=wd)
            val_loss = self.loss_fn(prediction, data_dict.val_masks.long())
            grad_output = torch.autograd.grad(val_loss,
                                              [weight] + list(self.decoder.parameters()), retain_graph=True,
                                              create_graph=False, allow_unused=True)
            seg_weight_grad, decoder_grads = grad_output[0], grad_output[1:]
            mean_iou = calc_iou_per_class(prediction, data_dict.val_masks)
            return val_loss, seg_weight_grad, decoder_grads, mean_iou, weight, prediction
        else:
            with torch.no_grad():
                mean_ious = []
                val_losses = []
                val_img_paths = data_dict.val_imgs
                val_mask_paths = data_dict.val_masks
                
                if config.train:
                    for _img_path, _mask_path in zip(val_img_paths, val_mask_paths):
                        input_img = numpy_to_tensor(list_to_tensor(_img_path, img_transformer))
                        input_mask = numpy_to_tensor(list_to_tensor(_mask_path, mask_transformer))
                        _, _, prediction = self.forward(input_img, weight=weight, we=we, wd=wd)
                        val_loss = self.loss_fn(prediction, input_mask.long()).item()
                        mean_iou = calc_iou_per_class(prediction, input_mask)
                        mean_ious.append(mean_iou)
                        val_losses.append(val_loss)
                    mean_iou = np.mean(mean_ious)
                    val_loss = np.mean(val_losses)
                    return val_loss, None, None, mean_iou, weight, prediction
                else:
                    prediction = []
                    for i in range(len(data_dict.val_imgs)):
                        input_img = torch.unsqueeze(data_dict.val_imgs[i], 0)
                        _, _, pred = self.forward(data_dict.val_imgs, weight=weight, we=we, wd=wd)
                        prediction.append(pred)
                    prediction  = torch.stack(prediction)
                    return prediction



def compute_loss(leo, metadata, train_stats, transformers, mode="meta_train"):
    """ Performs meta optimization across tasks
        returns the meta validation loss across tasks
        Args:
            metadata(dict): dictionary containing training data
            train_stats(object): object that stores training statistics
            transformers(tuple): tuple of image and mask transformers
            mode(str): meta_train, meta_val or meta_test
        Returns:
            total_val_loss(float32): meta-validation loss
            train_stats(object): object that stores training statistics
    """
    num_tasks = len(metadata[0])
    # initialize decoder on the first episode
    if train_stats.episode == 1:
        data_dict = get_named_dict(metadata, 0)
        skip_features, latents = leo.forward_encoder(data_dict.tr_imgs, mode)
        leo.decoder = DecoderBlock(skip_features, latents).to(device)
        leo.optimizer_decoder = torch.optim.Adam(
            leo.decoder.parameters(), lr=hyp.outer_loop_lr)

    if train_stats.episode % config.display_stats_interval == 1:
        display_data_shape(metadata)

    classes = metadata[4]
    total_val_loss = []
    mean_iou_dict = {}
    total_grads = None
    leo.forward_encoder
    for batch in range(num_tasks):
        data_dict = get_named_dict(metadata, batch)
        seg_weight_grad, features, we, wd = leo.leo_inner_loop(data_dict.tr_imgs, data_dict.tr_masks)
        val_loss, seg_weight_grad, decoder_grads, mean_iou, _, _ = \
            leo.finetuning_inner_loop(data_dict, features, seg_weight_grad,
                                      transformers, mode, we=we, wd=wd)
        if mode == "meta_train":
            decoder_grads_ = []
            for grad in decoder_grads:
                if grad is not None:
                    decoder_grads_.append(grad / num_tasks)
                else:
                    decoder_grads_.append(0)

            if total_grads is None:
                total_grads = decoder_grads_
                seg_weight_grad = seg_weight_grad / num_tasks
            else:
                total_grads = [total_grads[i] + decoder_grads_[i] \
                               for i in range(len(decoder_grads_))]
                seg_weight_grad += seg_weight_grad / num_tasks

            for i in range(len(total_grads)):
                if type(total_grads[i]) is not int:
                    total_grads[i] = torch.clamp(total_grads[i], min=-hyp.max_grad_norm, max=hyp.max_grad_norm)

        mean_iou_dict[classes[batch]] = mean_iou
        total_val_loss.append(val_loss)

    if mode == "meta_train":
        leo.optimizer_decoder.zero_grad()
        leo.optimizer_seg_network.zero_grad()

        for i, params in enumerate(leo.decoder.parameters()):
            try:
                params.grad = total_grads[i]
            except:
                params.grad = None

        leo.seg_weight.grad = seg_weight_grad
        leo.optimizer_decoder.step()
        leo.optimizer_seg_network.step()
    total_val_loss = float(sum(total_val_loss) / len(total_val_loss))
    stats_data = {
        "mode": mode,
        "total_val_loss": total_val_loss,
        "mean_iou_dict": mean_iou_dict
    }
    train_stats.update_stats(**stats_data)
    return total_val_loss, train_stats


def save_model(model, config, train_stats):
    """
    Save the model while training based on check point interval
    if episode number is not -1 then a prompt to delete checkpoints occur if
    checkpoints for that episode number exits.
    This only occurs if the prompt_deletion flag in the experiment dictionary
    is true else checkpoints that already exists are automatically deleted
    Args:
        model - trained model
        optimizer - optimized weights
        config - global config
        stats - dictionary containing stats for the current episode
    Returns:
    """
    with torch.no_grad():
        model.seg_network.weight.copy_(model.seg_weight.data.detach())

    data_to_save = {
        'model_dict': model.state_dict(),
        'optimizer_decoder_dict': model.optimizer_decoder.state_dict(),
        'optimizer_seg_network_dict': model.optimizer_seg_network.state_dict(),
        'train_stats': train_stats,
    }

    experiment = config.experiment
    model_root = os.path.join(os.path.dirname(__file__), config.data_path, "models")
    model_dir = os.path.join(model_root, "experiment_{}" \
                             .format(experiment.number))
    checkpoint_path = os.path.join(model_dir, f"checkpoint_{train_stats.episode}.pt")
    if not os.path.exists(checkpoint_path):
        torch.save(data_to_save, checkpoint_path)
    else:
        os.remove(checkpoint_path)
        torch.save(data_to_save, checkpoint_path)


def load_model(device, data_dict):
    """
    Loads the model
    Args:
        config - global config
        **************************************************
        Note: The episode key in the experiment dict
        implies the checkpoint that should be loaded for 
        inference purposes
        *************************************************
    Returns:
        leo :loaded model that was saved
        optimizer: loaded weights of optimizer
        stats: stats for the last saved model
    """
    experiment = config.experiment
    model_root = os.path.join(os.path.dirname(__file__), config.data_path, "models")
    model_dir = os.path.join(model_root, "experiment_{}" \
                             .format(experiment.number))
    checkpoints = os.listdir(model_dir)
    checkpoints = [i for i in checkpoints if os.path.splitext(i)[-1] == ".pt"]
    available_checkpoints = [int(cp.split(".")[0].split("_")[1]) for cp in checkpoints]
    selected_checkpoint = max(available_checkpoints)
    if not config.train:
        if experiment.episode not in available_checkpoints:
            print("Selected episode not availabe for loading")
            selected_checkpoint = int(input(f"Select one of the following: {available_checkpoints}"))
            if selected_checkpoint not in available_checkpoints:
                raise ValueError("Stop being silly and select the right value")
         
        print(f"Episode {selected_checkpoint} selected for Evaluation")
    checkpoint_path = os.path.join(model_dir, f"checkpoint_{selected_checkpoint}.pt")
    checkpoint = torch.load(checkpoint_path)
    leo = LEO().to(device)
    skip_features, latents = leo.forward_encoder(data_dict.tr_imgs, mode="meta_train")
    leo.decoder = DecoderBlock(skip_features, latents).to(device)
    leo.optimizer_decoder = torch.optim.Adam(
                    leo.decoder.parameters(), lr=hyp.outer_loop_lr)

   
    leo.optimizer_decoder.load_state_dict(checkpoint['optimizer_decoder_dict'])
    leo.optimizer_seg_network.load_state_dict(checkpoint['optimizer_seg_network_dict'])
    leo.load_state_dict(checkpoint['model_dict'])
    train_stats = checkpoint['train_stats']
    leo.seg_weight = leo.seg_network.weight.detach().to(device)
    leo.seg_weight.requires_grad = True
    if config.train:
        train_stats.update_after_restart()

    return leo, train_stats

