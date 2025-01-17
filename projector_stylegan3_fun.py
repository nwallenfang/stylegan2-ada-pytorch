"""
This script is taken from
    https://github.com/PDillis/stylegan3-fun
NVIDIA StyleGAN license
"""
import copy
from pathlib import Path
from time import perf_counter
from typing import Tuple

import PIL.Image
import numpy as np
import torch
# noinspection PyPep8Naming
import torch.nn.functional as F
import torchvision.transforms

from dnnlib.util import format_time
from metrics import metric_utils
from sample_augment.models.stylegan2 import gen_utils
from sample_augment.models.stylegan2.network_features import VGG16FeaturesNVIDIA, DiscriminatorFeatures
from sample_augment.models.stylegan2.ssim import SSIM  # from https://github.com/Po-Hsun-Su/pytorch-ssim
from sample_augment.utils import log
from sample_augment.utils.path_utils import shared_dir


# noinspection PyUnboundLocalVariable
def project(
        G,
        target: PIL.Image.Image,  # [C,H,W] and dynamic range [0,255], W & H must match G output resolution
        target_class: torch.Tensor,  # [1, G.c_dim]
        *,
        projection_seed: int,
        truncation_psi: float,
        num_steps: int = 1000,
        w_avg_samples: int = 10000,
        initial_learning_rate: float = 0.1,
        initial_noise_factor: float = 0.05,
        constant_learning_rate: bool = False,
        lr_rampdown_length: float = 0.25,
        lr_rampup_length: float = 0.05,
        noise_ramp_length: float = 0.75,
        regularize_noise_weight: float = 1e5,
        project_in_wplus: bool = True,  # clip would be worth trying I feel like
        loss_paper: str = 'im2sgan',  # ['sgan2' || Experimental: 'im2sgan' | 'clip' | 'discriminator']
        normed: bool = True,
        sqrt_normed: bool = False,
        start_wavg: bool = True,
        device: torch.device,
        D=None) -> Tuple[torch.Tensor, dict]:  # output shape: [num_steps, C, 512], C depending on resolution of G
    """
    Projecting a 'target' image into the W latent space. The user has an option to project into W+, where all elements
    in the latent vector are different. Likewise, the projection process can start from the W midpoint or from a random
    point, though results have shown that starting from the midpoint (start_wavg) yields the best results.
    """
    assert target.size == (G.img_resolution, G.img_resolution)

    G = copy.deepcopy(G).eval().requires_grad_(False).to(device)

    # Compute w stats.
    # TODO enable conditional support
    z_samples = np.random.RandomState(123).randn(w_avg_samples, G.z_dim)
    if target_class.ndim == 1:
        target_class = target_class.unsqueeze(0)
    assert target_class.shape == (1, G.c_dim), f"expected shape {(1, G.c_dim)}, got {target_class.shape}"
    target_class = target_class.to(device)
    w_samples = G.mapping(torch.from_numpy(z_samples).to(device), target_class.repeat(w_avg_samples, 1))  # [N, L, C]
    if project_in_wplus:  # Thanks to @pbaylies for a clean way on how to do this
        print('Projecting in W+ latent space...')
        if start_wavg:
            print(f'Starting from W midpoint using {w_avg_samples} samples...')
            w_avg = torch.mean(w_samples, dim=0, keepdim=True)  # [1, L, C]
        else:
            print(f'Starting from a random vector (seed: {projection_seed})...')
            z = np.random.RandomState(projection_seed).randn(1, G.z_dim)
            w_avg = G.mapping(torch.from_numpy(z).to(device), target_class)  # [1, L, C]
            w_avg = G.mapping.w_avg + truncation_psi * (w_avg - G.mapping.w_avg)
    else:
        print('Projecting in W latent space...')
        w_samples = w_samples[:, :1, :]  # [N, 1, C]
        if start_wavg:
            print(f'Starting from W midpoint using {w_avg_samples} samples...')
            w_avg = torch.mean(w_samples, dim=0, keepdim=True)  # [1, 1, C]
        else:
            print(f'Starting from a random vector (seed: {projection_seed})...')
            z = np.random.RandomState(projection_seed).randn(1, G.z_dim)
            w_avg = G.mapping(torch.from_numpy(z).to(device), target_class)[:, :1, :]  # [1, 1, C]; fake w_avg
            w_avg = G.mapping.w_avg + truncation_psi * (w_avg - G.mapping.w_avg)
    w_std = (torch.sum((w_samples - w_avg) ** 2) / w_avg_samples) ** 0.5
    # Setup noise inputs (only for StyleGAN2 models)
    noise_buffs = {name: buf for (name, buf) in G.synthesis.named_buffers() if 'noise_const' in name}

    # Features for target image. Reshape to 256x256 if it's larger to use with VGG16
    # (unnecessary for CLIP due to preprocess step)
    if loss_paper in ['sgan2', 'im2sgan', 'discriminator']:
        # noinspection PyTypeChecker
        target = np.array(target, dtype=np.uint8)
        target = torch.tensor(target.transpose([2, 0, 1]), device=device)
        target = target.unsqueeze(0).to(device).to(torch.float32)
        if target.shape[2] > 256:
            target = F.interpolate(target, size=(256, 256), mode='area')

    if loss_paper in ['sgan2', 'im2sgan']:
        # Load the VGG16 feature detector.
        url = 'https://nvlabs-fi-cdn.nvidia.com/stylegan2-ada-pytorch/pretrained/metrics/vgg16.pt'
        vgg16 = metric_utils.get_feature_detector(url, device=device)

    # Define the target features and possible new losses
    if loss_paper == 'sgan2':
        target_features = vgg16(target, resize_images=False, return_lpips=True)
    elif loss_paper == 'im2sgan':
        # Use specific layers
        vgg16_features = VGG16FeaturesNVIDIA(vgg16)
        # Too cumbersome to add as command-line arg, so we leave it here; use whatever you need, as many times as needed
        layers = ['conv1_1', 'conv1_2', 'conv2_1', 'conv2_2', 'conv3_1', 'conv3_2', 'conv3_3', 'conv4_1', 'conv4_2',
                  'conv4_3', 'conv5_1', 'conv5_2', 'conv5_3', 'fc1', 'fc2', 'fc3']
        target_features = vgg16_features.get_layers_features(target, layers, normed=normed, sqrt_normed=sqrt_normed)
        # Uncomment the next line if you also want to use LPIPS features
        # lpips_target_features = vgg16(target_images, resize_images=False, return_lpips=True)

        mse = torch.nn.MSELoss(reduction='mean')
        ssim_out = SSIM()  # can be used as a loss; recommended usage: ssim_loss = 1 - ssim_out(img1, img2)

    elif loss_paper == 'discriminator':
        disc = DiscriminatorFeatures(D).requires_grad_(False).to(device)

        layers = ['b128_conv0', 'b128_conv1', 'b64_conv0', 'b64_conv1', 'b32_conv0', 'b32_conv1',
                  'b16_conv0', 'b16_conv1', 'b8_conv0', 'b8_conv1', 'b4_conv']

        target_features = disc.get_layers_features(target, layers, normed=normed, sqrt_normed=sqrt_normed)
        mse = torch.nn.MSELoss(reduction='mean')
        ssim_out = SSIM()

    elif loss_paper == 'clip':
        import clip
        model, preprocess = clip.load('ViT-B/32',
                                      device=device)

        target = preprocess(target).unsqueeze(0).to(device)
        # text = either we give a target image or a text as target
        target_features = model.encode_image(target)

        mse = torch.nn.MSELoss(reduction='mean')

    w_opt = w_avg.clone().detach().requires_grad_(True)
    w_out = torch.zeros([num_steps] + list(w_opt.shape[1:]), dtype=torch.float32, device=device)
    optimizer = torch.optim.Adam([w_opt] + list(noise_buffs.values()), betas=(0.9, 0.999), lr=initial_learning_rate)

    # Init noise.
    for buf in noise_buffs.values():
        buf[:] = torch.randn_like(buf)
        buf.requires_grad = True

    for step in range(num_steps):
        # Learning rate schedule.
        t = step / num_steps
        w_noise_scale = w_std * initial_noise_factor * max(0.0, 1.0 - t / noise_ramp_length) ** 2

        if constant_learning_rate:
            # Turn off the rampup/rampdown of the learning rate
            lr_ramp = 1.0
        else:
            lr_ramp = min(1.0, (1.0 - t) / lr_rampdown_length)
            lr_ramp = 0.5 - 0.5 * np.cos(lr_ramp * np.pi)
            lr_ramp = lr_ramp * min(1.0, t / lr_rampup_length)
        lr = initial_learning_rate * lr_ramp
        for param_group in optimizer.param_groups:
            param_group['lr'] = lr

        # Synth images from opt_w.
        w_noise = torch.randn_like(w_opt) * w_noise_scale
        if project_in_wplus:
            ws = w_opt + w_noise
        else:
            ws = (w_opt + w_noise).repeat([1, G.mapping.num_ws, 1])
        synth_images = G.synthesis(ws, noise_mode='const')

        # Downsample image to 256x256 if it's larger than that. VGG was built for 224x224 images.
        synth_images = (synth_images + 1) * (255 / 2)
        if synth_images.shape[2] > 256:
            print("downsample bruhh.")
            synth_images = F.interpolate(synth_images, size=(256, 256), mode='area')

        # Reshape synthetic images if G was trained with grayscale data
        if synth_images.shape[1] == 1:
            print("gray scale bruh")
            synth_images = synth_images.repeat(1, 3, 1, 1)  # [1, 1, 256, 256] => [1, 3, 256, 256]

        # Features for synth images.
        if loss_paper == 'sgan2':
            synth_features = vgg16(synth_images, resize_images=False, return_lpips=True)
            dist = (target_features - synth_features).square().sum()

            # Noise regularization.
            reg_loss = 0.0
            for v in noise_buffs.values():
                noise = v[None, None, :, :]  # must be [1,1,H,W] for F.avg_pool2d()
                while True:
                    reg_loss += (noise * torch.roll(noise, shifts=1, dims=3)).mean() ** 2
                    reg_loss += (noise * torch.roll(noise, shifts=1, dims=2)).mean() ** 2
                    if noise.shape[2] <= 8:
                        break
                    noise = F.avg_pool2d(noise, kernel_size=2)
            loss = dist + reg_loss * regularize_noise_weight
            # Print in the same line (avoid cluttering the commandline)
            n_digits = int(np.log10(num_steps)) + 1 if num_steps > 0 else 1
            message = f'step {step + 1:{n_digits}d}/{num_steps}: dist {dist:.3e} | loss {loss.item():.3e}'
            print(message)

            last_status = {'dist': dist.item(), 'loss': loss.item()}

        elif loss_paper == 'im2sgan':
            # Uncomment to also use LPIPS features as loss (must be better fine-tuned):
            # lpips_synth_features = vgg16(synth_images, resize_images=False, return_lpips=True)

            synth_features = vgg16_features.get_layers_features(synth_images, layers, normed=normed,
                                                                sqrt_normed=sqrt_normed)
            percept_error = sum(map(lambda x, y: mse(x, y), target_features, synth_features))

            # Also uncomment to add the LPIPS loss to the perception error (to-be better fine-tuned)
            # percept_error += 1e1 * (lpips_target_features - lpips_synth_features).square().sum()

            # Pixel-level MSE
            mse_error = mse(synth_images, target) / (G.img_channels * G.img_resolution * G.img_resolution)
            ssim_loss = ssim_out(target, synth_images)  # tracking SSIM (can also be added the total loss)
            loss = percept_error + mse_error  # + 1e-2 * (1 - ssim_loss)  # needs to be fine-tuned

            # Noise regularization.
            reg_loss = 0.0
            for v in noise_buffs.values():
                noise = v[None, None, :, :]  # must be [1,1,H,W] for F.avg_pool2d()
                while True:
                    reg_loss += (noise * torch.roll(noise, shifts=1, dims=3)).mean() ** 2
                    reg_loss += (noise * torch.roll(noise, shifts=1, dims=2)).mean() ** 2
                    if noise.shape[2] <= 8:
                        break
                    noise = F.avg_pool2d(noise, kernel_size=2)
            loss += reg_loss * regularize_noise_weight
            # We print in the same line (avoid cluttering the commandline)
            n_digits = int(np.log10(num_steps)) + 1 if num_steps > 0 else 1
            message = f'step {step + 1:{n_digits}d}/{num_steps}: percept loss {percept_error:.3e} | ' \
                      f'pixel mse {mse_error.item():.3e} | ssim {ssim_loss.item():.3e} | loss {loss.item():.3e}'
            print(message)  # , end='\r')
            # print(torch.min(synth_images), torch.max(synth_images))

            # print(synth_images[0, 0, 100, 100:150])
            last_status = {'percept_error': percept_error,
                           'pixel_mse': mse_error.item(),
                           'ssim': ssim_loss.item(),
                           'loss': loss.item()}

        elif loss_paper == 'discriminator':
            synth_features = disc.get_layers_features(synth_images, layers, normed=normed, sqrt_normed=sqrt_normed)
            percept_error = sum(map(lambda x, y: mse(x, y), target_features, synth_features))

            # Also uncomment to add the LPIPS loss to the perception error (to-be better fine-tuned)
            # percept_error += 1e1 * (lpips_target_features - lpips_synth_features).square().sum()

            # Pixel-level MSE
            mse_error = mse(synth_images, target) / (G.img_channels * G.img_resolution * G.img_resolution)
            ssim_loss = ssim_out(target, synth_images)  # tracking SSIM (can also be added the total loss)
            loss = percept_error + mse_error  # + 1e-2 * (1 - ssim_loss)  # needs to be fine-tuned

            # Noise regularization.
            reg_loss = 0.0
            for v in noise_buffs.values():
                noise = v[None, None, :, :]  # must be [1,1,H,W] for F.avg_pool2d()
                while True:
                    reg_loss += (noise * torch.roll(noise, shifts=1, dims=3)).mean() ** 2
                    reg_loss += (noise * torch.roll(noise, shifts=1, dims=2)).mean() ** 2
                    if noise.shape[2] <= 8:
                        break
                    noise = F.avg_pool2d(noise, kernel_size=2)
            loss += reg_loss * regularize_noise_weight
            # We print in the same line (avoid cluttering the commandline)
            n_digits = int(np.log10(num_steps)) + 1 if num_steps > 0 else 1
            message = f'step {step + 1:{n_digits}d}/{num_steps}: percept loss {percept_error:.7e} | ' \
                      f'pixel mse {mse_error.item():.7e} | ssim {ssim_loss.item():.7e} | loss {loss.item():.7e}'
            print(message, end='\r')

            last_status = {'percept_error': percept_error,
                           'pixel_mse': mse_error.item(),
                           'ssim': ssim_loss.item(),
                           'loss': loss.item()}

        elif loss_paper == 'clip':

            import torchvision.transforms as transforms
            synth_img = F.interpolate(synth_images, size=(224, 224), mode='area')
            prep = transforms.Normalize(mean=(0.48145466, 0.4578275, 0.40821073),
                                        std=(0.26862954, 0.26130258, 0.27577711))
            synth_img = prep(synth_img)
            # NCWH => WHC
            # synth_images = synth_images.permute(0, 2, 3, 1).clamp(0, 255).to(torch.uint8).cpu().numpy()[0]
            synth_features = model.encode_image(synth_img)
            dist = mse(target_features, synth_features)

            # Noise regularization.
            reg_loss = 0.0
            for v in noise_buffs.values():
                noise = v[None, None, :, :]  # must be [1,1,H,W] for F.avg_pool2d()
                while True:
                    reg_loss += (noise * torch.roll(noise, shifts=1, dims=3)).mean() ** 2
                    reg_loss += (noise * torch.roll(noise, shifts=1, dims=2)).mean() ** 2
                    if noise.shape[2] <= 8:
                        break
                    noise = F.avg_pool2d(noise, kernel_size=2)
            loss = dist + reg_loss * regularize_noise_weight
            # Print in the same line (avoid cluttering the commandline)
            n_digits = int(np.log10(num_steps)) + 1 if num_steps > 0 else 1
            message = f'step {step + 1:{n_digits}d}/{num_steps}: dist {dist:.7e}'
            print(message)

            last_status = {'dist': dist.item(), 'loss': loss.item()}

        # def save_tensor_as_image(tensor):
        #     # tensor shape is [batch_size, channels, height, width] i.e., [1, 3, 256, 256]
        #     tensor = tensor.squeeze(0)  # remove batch dimension
        #     # if your tensor has values range [0,1], convert it to [0,255]
        #     # tensor = tensor.mul(255).byte()
        #     tensor = (tensor - tensor.min()) / (tensor.max() - tensor.min())
        #     # if your tensor is on GPU, bring it back to CPU
        #     tensor = tensor.cpu()
        #     img = torchvision.transforms.ToPILImage()(tensor)  # convert tensor to PIL image
        #     img.save(shared_dir / "projected" / "hurz.png")
        #
        # save_tensor_as_image(synth_images)

        # Step
        optimizer.zero_grad(set_to_none=True)
        loss.backward(retain_graph=True)  # retain graph should not be needed but got an err
        optimizer.step()

        # Save projected W for each optimization step.
        w_out[step] = w_opt.detach()[0]

        # Normalize noise.
        with torch.no_grad():
            for buf in noise_buffs.values():
                buf -= buf.mean()
                buf *= buf.square().mean().rsqrt()

    # Save run config
    run_config = {
        'optimization_options': {
            'num_steps': num_steps,
            'initial_learning_rate': initial_learning_rate,
            'constant_learning_rate': constant_learning_rate,
            'regularize_noise_weight': regularize_noise_weight,
        },
        'projection_options': {
            'w_avg_samples': w_avg_samples,
            'initial_noise_factor': initial_noise_factor,
            'lr_rampdown_length': lr_rampdown_length,
            'lr_rampup_length': lr_rampup_length,
            'noise_ramp_length': noise_ramp_length,
        },
        'latent_space_options': {
            'project_in_wplus': project_in_wplus,
            'start_wavg': start_wavg,
            'projection_seed': projection_seed,
            'truncation_psi': truncation_psi,
        },
        'loss_options': {
            'loss_paper': loss_paper,
            'vgg16_normed': normed,
            'vgg16_sqrt_normed': sqrt_normed,
        },
        'elapsed_time': '',
        'last_commandline_status': last_status
    }

    if project_in_wplus:
        return w_out, run_config  # [num_steps, L, C]
    return w_out.repeat([1, G.mapping.num_ws, 1]), run_config  # [num_steps, 1, C] => [num_steps, L, C]


# ----------------------------------------------------------------------------


# @click.command() @click.pass_context @click.option('--network', '-net', 'network_pkl', help='Network pickle
# filename', required=True) @click.option('--cfg', help='Config of the network, used only if you want to use one of
# the models that are in torch_utils.gen_utils.resume_specs', type=click.Choice(['stylegan2', 'stylegan3-t',
# 'stylegan3-r'])) @click.option('--target', '-t', 'target_fname', type=click.Path(exists=True, dir_okay=False),
# help='Target image file to project to', required=True, metavar='FILE') # Optimization options @click.option(
# '--num-steps', '-nsteps', help='Number of optimization steps', type=click.IntRange(min=0), default=1000,
# show_default=True) @click.option('--init-lr', '-lr', 'initial_learning_rate', type=float, help='Initial learning
# rate of the optimization process', default=0.1, show_default=True) @click.option('--constant-lr',
# 'constant_learning_rate', is_flag=True, help='Add flag to use a constant learning rate throughout the optimization
# (turn off the rampup/rampdown)') @click.option('--reg-noise-weight', '-regw', 'regularize_noise_weight',
# type=float, help='Noise weight regularization', default=1e5, show_default=True) @click.option('--seed', type=int,
# help='Random seed', default=303, show_default=True) @click.option('--stabilize-projection', is_flag=True,
# help='Add flag to stabilize the latent space/anchor to w_avg, making it easier to project (only for StyleGAN3
# config-r/t models)') # Video options @click.option('--save-video', '-video', is_flag=True, help='Save an mp4 video
# of optimization progress') @click.option('--compress', is_flag=True, help='Compress video with ffmpeg-python; same
# resolution, lower memory size') @click.option('--fps', type=int, help='FPS for the mp4 video of optimization
# progress (if saved)', default=30, show_default=True) # Options on which space to project to (W or W+) and where to
# start: the middle point of W (w_avg) or a specific seed @click.option('--project-in-wplus', '-wplus', is_flag=True,
# help='Project in the W+ latent space') @click.option('--start-wavg', '-wavg', type=bool, help='Start with the
# average W vector, ootherwise will start from a random seed (provided by user)', default=True, show_default=True)
# @click.option('--projection-seed', type=int, help='Seed to start projection from', default=None, show_default=True)
# @click.option('--trunc', 'truncation_psi', type=float, help='Truncation psi to use in projection when using a
# projection seed', default=0.7, show_default=True) # Decide the loss to use when projecting (all other apart from
# o.g. StyleGAN2's are experimental, you can select the VGG16 features/layers to use in the im2sgan loss)
# @click.option('--loss-paper', '-loss', type=click.Choice(['sgan2', 'im2sgan', 'discriminator', 'clip']),
# help='Loss to use (if using "im2sgan", make sure to norm the VGG16 features)', default='sgan2', show_default=True)
# im2sgan loss options (try with and without them, though I've found --vgg-normed to work best for me) @click.option(
# '--vgg-normed', 'normed', is_flag=True, help='Add flag to norm the VGG16 features by the number of elements per
# layer that was used') @click.option('--vgg-sqrt-normed', 'sqrt_normed', is_flag=True, help='Add flag to norm the
# VGG16 features by the square root of the number of elements per layer that was used') # Extra parameters for saving
# the results @click.option('--save-every-step', '-saveall', is_flag=True, help='Save every step taken in the
# projection (save both the dlatent as a.npy and its respective image).') @click.option('--outdir', type=click.Path(
# file_okay=False), help='Directory path to save the results', default=os.path.join(os.getcwd(), 'out',
# 'projection'), show_default=True, metavar='DIR') @click.option('--description', '-desc', type=str, help='Extra
# description to add to the experiment name', default='')
def run_projection_sgan3_fun(
        G,
        target_image: np.ndarray,  # [C,H,W] and dynamic range [0,255], W & H must match G output resolution
        target_class: torch.Tensor,
        identifier: str,
        seed: int,
        out_dir: Path,  # where to save the results :)
        regularize_noise_weight: float = 1e3,  # 1e5,
        num_steps: int = 1000,
        initial_learning_rate: float = 0.01,
        constant_learning_rate: bool = False,
        stabilize_projection: bool = False,  # only for StyleGAN3 models (so not for us)
        project_in_wplus: bool = True,  # def wanna try this
        start_wavg: bool = True,  # same for this
        projection_seed: int = None,  # ? see usage
        truncation_psi: float = 0.95,  # "when using projection seed"
        loss_paper: str = 'im2sgan',  # ['sgan2', 'im2sgan', 'discriminator', 'clip'],
        normed: bool = True,  # VGG16 feature norm, (if using "im2sgan", make sure to norm the VGG16 features)
        sqrt_normed: bool = False,  # sqrt VGG16 feature norm

        description: str = '',
):
    """Project given image to the latent space of pretrained network pickle.

    Examples:

    \b
    python projector.py --target=~/mytarget.png --project-in-wplus --save-video --num-steps=5000 \\
        --network=https://nvlabs-fi-cdn.nvidia.com/stylegan2-ada-pytorch/pretrained/ffhq.pkl
    """
    torch.manual_seed(seed)

    # If we're not starting from the W midpoint, assert the user fed a seed to start from
    if not start_wavg:
        if projection_seed is None:
            log.error(
                'Provide a seed to start from if not starting from the midpoint. Use "--projection-seed" to do so')

    device = torch.device('cuda')
    # with dnnlib.util.open_url(network_pkl) as fp:
    #     G = legacy.load_network_pkl(fp)['G_ema'].requires_grad_(False).to(device)
    if loss_paper == 'discriminator':
        raise ValueError('not supported for now')
        # We must also load the Discriminator
        # with dnnlib.util.open_url(network_pkl) as fp:
        #     D = legacy.load_network_pkl(fp)['D'].requires_grad_(False).to(device)

    # Load target image.
    # target_pil = PIL.Image.open(target_fname).convert('RGB')
    # w, h = target_pil.size
    # s = min(w, h)
    # target_pil = target_pil.crop(((w - s) // 2, (h - s) // 2, (w + s) // 2, (h + s) // 2))
    # target_pil = target_pil.resize((G.img_resolution, G.img_resolution), PIL.Image.LANCZOS)
    # noinspection PyTypeChecker

    target_uint8 = PIL.Image.fromarray(np.transpose(target_image, (1, 2, 0)))

    # Stabilize the latent space to make things easier (for StyleGAN3's config t and r models)
    if stabilize_projection:
        gen_utils.anchor_latent_space(G)

    # Optimize projection.
    start_time = perf_counter()
    projected_w_steps, run_config = project(
        G,
        target=target_uint8,
        target_class=target_class,
        num_steps=num_steps,
        initial_learning_rate=initial_learning_rate,
        constant_learning_rate=constant_learning_rate,
        regularize_noise_weight=regularize_noise_weight,
        project_in_wplus=project_in_wplus,
        start_wavg=start_wavg,
        projection_seed=projection_seed,
        truncation_psi=truncation_psi,
        loss_paper=loss_paper,
        normed=normed,
        sqrt_normed=sqrt_normed,
        device=device,
        # D=D if loss_paper == 'discriminator' else None
    )
    elapsed_time = format_time(perf_counter() - start_time)
    print(f'\nElapsed time: {elapsed_time}')
    run_config['elapsed_time'] = elapsed_time
    # Make the run dir automatically
    desc = 'projection-wplus' if project_in_wplus else 'projection-w'
    desc = f'{desc}-wavgstart' if start_wavg else f'{desc}-seed{projection_seed}start'
    desc = f'{desc}-{description}' if len(description) != 0 else desc
    desc = f'{desc}-{loss_paper}'
    # run_dir = gen_utils.make_run_dir(out_dir, desc)

    # Save the configuration used
    # obj = {
    #     'description': description,
    #     'target_image': 'test',
    #     'target_class': target_class.cpu().tolist(),
    #     'outdir': run_dir,
    #     'seed': seed,
    #     'run_config': run_config
    # }
    # # Save the run configuration
    # gen_utils.save_config(obj, run_dir=run_dir)

    # Render debug output: optional video and projected image and W vector.
    result_name = 'proj'
    npz_name = 'latent'
    # If we project in W+, add to the name of the results
    if project_in_wplus:
        result_name += '_wplus'
        npz_name += '_wplus'
    # Either in W or W+, we can start from the W midpoint or one given by the projection seed
    if start_wavg:
        result_name, npz_name = f'{result_name}_wavg', f'{npz_name}_wavg'
    else:
        result_name, npz_name = f'{result_name}_seed-{projection_seed}', f'{npz_name}_seed-{projection_seed}'

    # print("target_uint8:", target_uint8.size)
    # Save the target image
    target_uint8.save(out_dir / f'target_{identifier}.png')

    # Save only the final projected frame and W vector.
    projected_w = projected_w_steps[-1]
    synth_image = gen_utils.w_to_img(G, dlatents=projected_w, noise_mode='const')[0]
    print(synth_image[1, 100, 100: 150])
    print(synth_image.shape, synth_image.dtype)
    PIL.Image.fromarray(synth_image, 'RGB').save(out_dir / f'proj_{identifier}.png')
    target_class_numpy = target_class.cpu().numpy()
    np.savez(str(out_dir / f'{npz_name}_{identifier}.npz'), w=projected_w.unsqueeze(0).cpu().numpy(),
             target_class=target_class_numpy)
