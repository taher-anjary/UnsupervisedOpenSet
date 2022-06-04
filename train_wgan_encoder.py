import os
import torch
import torch.nn as nn
import torch.nn.functional as F
import torchvision
import torchvision.transforms as T
from torch.utils.data import Dataset, DataLoader
import matplotlib.pyplot as plt
from tqdm import tqdm
import numpy as np
import random
import wandb
from torchvision.utils import make_grid, save_image
from torch.optim.lr_scheduler import StepLR, ReduceLROnPlateau

from model.encoder import Encoder
from model.wgan import WGAN_GP
from model.utils import to_img
from data.dataset_maker import split_dataset, get_unk_dataset

torch.manual_seed(0)
torch.cuda.manual_seed(0)
np.random.seed(0)
random.seed(0)

def to_4d(x):
    return x.unsqueeze(-1).unsqueeze(-1)

def train(encoder, generator, dataloader, optimizer, loss_fn, device):
    encoder.train()
    generator.train()
    train_losses = []

    for batch, _ in tqdm(dataloader):
        batch = batch.float().to(device)
        optimizer.zero_grad()
        loss = loss_fn(generator.G(to_4d(encoder(batch))), batch)
        loss.backward()
        optimizer.step()
        train_losses.append(loss.item())

    return train_losses


def evaluate(encoder, generator, dataloader, loss_fn, device):
    encoder.eval()
    generator.eval()
    eval_losses = []

    with torch.no_grad():
        for batch, _ in tqdm(dataloader):
            batch = batch.float().to(device)
            loss = loss_fn(generator.G(to_4d(encoder(batch))), batch)
            eval_losses.append(loss.item())
    
    return sum(eval_losses) / len(eval_losses)
    

def vis(encoder, generator, dataloader, filename, config, device):
    imgs = next(iter(dataloader))[0][:20]
    out = generator.G(to_4d(encoder(imgs.to(device))))
    recons, org = to_img(out.cpu().data), to_img(imgs.cpu().data)
    merged = torch.stack((org,recons),dim=1).view(-1,3,32,32)
    save_image(merged, os.path.join(config.out_path, filename))


def main():

    configs = {
        "channels":3,
        "train": False,
        "cuda": True,
        "iters": 40000,
        "batch_size": 128,
        "learning_rate":1e-3,
        "betas":(0.5, 0.999),
        "epochs": 100,
        "split": 1,
        "ckpt_period":25,
        "type":"train wgan encoder",
        "vis_only":True
    }

    wandb.init(project="547_term", config=configs)
    config = wandb.config
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    ckpt_path = os.path.join("ckpt/wgan_encoder", "split" + str(config.split))
    out_path = os.path.join("output/wgan_encoder", "split" + str(config.split))
    config.out_path = out_path
    config.ckpt_path = ckpt_path

    # Prepare splitted dataset and loaders
    train_set = torchvision.datasets.CIFAR10(root='./dataset', train=True, download=True)
    val_set = torchvision.datasets.CIFAR10(root='./dataset', train=False, download=True)
    train_transform = T.Compose([T.ToPILImage(), T.RandomCrop(32, padding=4), T.RandomHorizontalFlip(), T.ToTensor(), T.Normalize((0.4914, 0.4822, 0.4465), (0.247, 0.243, 0.261))])
    val_transform = T.Compose([T.ToTensor(), T.Normalize((0.4914, 0.4822, 0.4465), (0.247, 0.243, 0.261))])
    split_train_set = split_dataset(train_set, config.split, train_transform)
    split_val_test = split_dataset(val_set, config.split, val_transform)
    train_loader = DataLoader(split_train_set, batch_size=config.batch_size, shuffle=True, num_workers=8)
    val_loader = DataLoader(split_val_test, batch_size=config.batch_size, shuffle=False, num_workers=8)


    encoder = Encoder().to(device)
    generator = WGAN_GP(config).to(device)
    d_path = "ckpt/wgan_gp/split"+str(config.split)+"/discriminator.pkl"
    g_path = "ckpt/wgan_gp/split"+str(config.split)+"/generator.pkl"
    generator.load_model(d_path, g_path)

    for param in list(generator.G.parameters()) + list(generator.D.parameters()):
        param.requires_grad = False

    loss_fn = nn.MSELoss(reduction="mean")
    optimizer = torch.optim.Adam(encoder.parameters(), lr=config.learning_rate, betas=config.betas, weight_decay=1e-4)
    scheduler = ReduceLROnPlateau(optimizer, mode="min", factor=0.5, patience=10, threshold_mode='abs')

    if not os.path.exists(out_path):
        os.makedirs(out_path)
    if not os.path.exists(ckpt_path):
        os.makedirs(ckpt_path)

    if config.vis_only:
        encoder = torch.load(os.path.join(ckpt_path, "ckpt100.pth"))
        
        known_vis_loader = DataLoader(split_val_test, batch_size=config.batch_size, shuffle=True, num_workers=8)
        vis(encoder, generator, known_vis_loader, "known_recons.png", config, device)
        
        known_l1, known_l2 = 0.0, 0.0
        for x,_ in tqdm(known_vis_loader):
            known_out = generator.G(to_4d(encoder(x.to(device))))
            known_l1 += F.l1_loss(known_out, x.to(device), reduction="sum").item()
            known_l2 += F.mse_loss(known_out, x.to(device), reduction="sum").item()

        unk_vis_set = get_unk_dataset(val_set, config.split, val_transform)
        unk_vis_loader = DataLoader(unk_vis_set, batch_size=config.batch_size, shuffle=True, num_workers=8)
        vis(encoder, generator, unk_vis_loader, "unk_recons.png", config, device)

        unk_l1, unk_l2 = 0.0, 0.0
        for x,_ in tqdm(unk_vis_loader):
            unk_out = generator.G(to_4d(encoder(x.to(device))))
            unk_l1 += F.l1_loss(unk_out, x.to(device), reduction="sum").item()
            unk_l2 += F.mse_loss(unk_out, x.to(device), reduction="sum").item()


        wandb.log({ "known L1 distance":known_l1, "unknown L1 distance":unk_l1,
                    "known mean L1 distance":known_l1/len(known_vis_loader.dataset), "unknown mean L1 distance":unk_l1/len(unk_vis_loader.dataset),
                    "known L2 distance":known_l2, "unknown L2 distance":unk_l2,
                    "known mean L2 distance":known_l2/len(known_vis_loader.dataset), "unknown mean L2 distance":unk_l2/len(unk_vis_loader.dataset)
        }) 
                    
        return 

    train_losses = []
    val_losses = []

    for i in range(config.epochs):
        train_loss = train(encoder, generator, train_loader, optimizer,loss_fn, device)
        val_loss = evaluate(encoder, generator, val_loader, loss_fn, device)
        scheduler.step(val_loss)

        train_losses.extend(train_loss)
        val_losses.append(val_loss)

        print('epoch [{}/{}], train loss:{:.4f}, val loss:{:.4f}'.format(i+1, config.epochs, sum(train_loss)/len(train_loss), val_loss))

        imgs = next(iter(val_loader))[0][:20]
        out = generator.G(to_4d(encoder(imgs.to(device))))
        recons, org = to_img(out.cpu().data), to_img(imgs.cpu().data)
        merged = torch.stack((org,recons),dim=1).view(-1,3,32,32)
        save_image(merged, os.path.join(out_path, "recons_epoch{}.png".format(i)))
        images = wandb.Image(merged, caption="Reconstructions")
        wandb.log({
            "wgan encoder - train loss":sum(train_loss)/len(train_loss), 
            "wgan encoder - val loss":val_loss, 
            "lr":optimizer.param_groups[0]['lr'],
            "reconstructions": images})

        if (i+1) % config.ckpt_period == 0:
            torch.save(encoder, os.path.join(ckpt_path, "ckpt"+str(i+1)+".pth"))


if __name__ == "__main__":
    main()
