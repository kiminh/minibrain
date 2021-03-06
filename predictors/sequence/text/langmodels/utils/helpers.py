"""
Helper functions for training and testing, for the moment many things contain absolute paths and is quite dirty
The thing is, I'll be improving the code as the research advances
"""

import os
import sys
import numpy as np
from datetime import datetime

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.tensorboard import SummaryWriter

from .preprocess_conllu import *


# from https://stackoverflow.com/questions/312443/how-do-you-split-a-list-into-evenly-sized-chunks
def chunks(data, n, dim=0):
    """Yield successive n-sized chunks from data by the dimension dim"""
    for i in range(0, data.shape[dim], n):
        yield data[i:i + n, :, :]


def pos_loss_function(upos, deprel, target_upos, target_deprel):
    # TODO check a more sofisticated loss function, for the moment only the sum to see if it runs
    # the issue is that upos is easier than deprel (18 vs 278 classes)
    # upos_loss = F.mse_loss(upos, target_upos)
    # deprel_loss = F.mse_loss(deprel, target_deprel)
    upos_loss = F.nll_loss(upos, target_upos.long())
    deprel_loss = F.nll_loss(deprel, target_deprel.long())
    # upos_loss = F.kl_div(upos, target_upos)
    # deprel_loss = F.kl_div(deprel, target_deprel)
    loss = upos_loss + deprel_loss
    # loss = F.kl_div(torch.cat([upos, deprel], dim=-1).contiguous(),
    #                 torch.cat([target_upos, target_deprel], dim=-1).contiguous())
    return loss, upos_loss, deprel_loss


writer = SummaryWriter()


def train(model, optimizer, loss_function, batches, epoch, ndatapoints, device, max_seq_len=-1):
    torch.cuda.empty_cache()
    model.train()
    train_loss = 0
    #     batch_loss = []
    batch_idx = 1
    for b_data in batches:
        b_train = torch.from_numpy(b_data[:, 0, :].astype("int32")).squeeze().to(device).long()
        # max_seq_len means I'll take care only of a part of the input to compare output. This is to save computation
        # only and not because of any other reason
        if max_seq_len > 0:
            b_upos = torch.from_numpy(b_data[:, 1, :max_seq_len].astype("int32")).squeeze().to(device).long()
            b_deprel = torch.from_numpy(b_data[:, 2, :max_seq_len].astype("int32")).squeeze().to(device).long()
        else:
            b_upos = torch.from_numpy(b_data[:, 1, :].astype("int32")).squeeze().to(device).long()
            b_deprel = torch.from_numpy(b_data[:, 2, :].astype("int32")).squeeze().to(device).long()
        #
        optimizer.zero_grad()
        dec = model(b_train)
        # last_latent = latent[-1]
        upos, deprel = dec
        # print(emb.shape,emb.dtype, res.shape, res.dtype)
        # print(upos.shape, b_upos.shape)
        # loss = loss_function(upos, deprel, upos_emb(b_upos), deprel_emb(b_deprel))
        # print("train tensor shapes: ", b_train.shape, upos.shape, b_upos.shape, deprel.shape, b_deprel.shape)
        loss, upos_loss, deprel_loss = loss_function(upos.view([-1, 18]), deprel.view([-1, 278]), b_upos.view([-1]), b_deprel.view([-1]))

        loss.backward()
        train_loss += loss.data.item()  # [0]
        writer.add_scalar("Loss/train", loss.data.item(), global_step=(epoch * batch_idx))
        writer.add_scalar("Loss/train/upos", upos_loss.data.item(), global_step=(epoch * batch_idx))
        writer.add_scalar("Loss/train/deprel", deprel_loss.data.item(), global_step=(epoch * batch_idx))
        optimizer.step()
        batch_idx += 1
        del b_train
        del b_upos
        del b_deprel
        torch.cuda.empty_cache()
    writer.add_scalar("EpochLoss/train", train_loss / batch_idx, epoch)
    print('====> Timestamp {} Epoch: {} Average loss: {:.8f}'.format(datetime.now(), epoch, train_loss / ndatapoints))
    return train_loss


def test(model, loss_function, test_data, epoch, device, max_data=40, max_seq_len=-1):
    """

    :param model:
    :param loss_function:
    :param test_data:
    :param epoch:
    :param device:
    :param max_data: maximum amout of data to test (default 50 due to gpu memory constraints in my pc)
    :return:
    """
    model.eval()
    test_loss = 0
    for lang, d in test_data:
        torch.cuda.empty_cache()  # make sure the cache is emptied to begin the nexxt batch
        b_test = torch.from_numpy(d[:max_data, 0, :].astype("int32")).squeeze().to(device).long()
        if max_seq_len > 0:
            b_upos = torch.from_numpy(d[:max_data, 1, :max_seq_len].astype("int32")).squeeze().to(device).int()  # .long()
            b_deprel = torch.from_numpy(d[:max_data, 2, :max_seq_len].astype("int32")).squeeze().to(device).int()  # .long()
        else:
            b_upos = torch.from_numpy(d[:max_data, 1, :].astype("int32")).squeeze().to(device).int()  # .long()
            b_deprel = torch.from_numpy(d[:max_data, 2, :].astype("int32")).squeeze().to(device).int()  # .long()
        upos, deprel = model(b_test)
        # loss = loss_function(upos.view([-1, 18]), b_upos.view([-1]))
        # print("test tensor shapes: ", b_test.shape, upos.shape, b_upos.shape, deprel.shape, b_deprel.shape)
        loss, upos_loss, deprel_loss = loss_function(upos.view([-1, 18]), deprel.view([-1, 278]), b_upos.view([-1]), b_deprel.view([-1]))
        test_loss += loss.data.item()
        writer.add_scalar("LangLoss/test/"+lang, loss.data.item(), global_step=epoch)
        writer.add_scalar("LangLoss/test/upos/"+lang, upos_loss.data.item(), global_step=epoch)
        writer.add_scalar("LangLoss/test/deprel/"+lang, deprel_loss.data.item(), global_step=epoch)
        del b_test
        del b_upos
        del b_deprel
        torch.cuda.empty_cache()
    test_loss /= len(test_data)  # although this is not faire as different languages give different results
    writer.add_scalar("EpochLangLoss/test/", test_loss, global_step=epoch)
    print('epoch: {}====> Test set loss: {:.8f}'.format(epoch, test_loss))


def test_accuracy(model, test_data, epoch, device, max_data=50):
    torch.cuda.empty_cache()  # make sure the cache is emptied
    model.eval()
    epoch_acc = 0

    upos_eye = torch.eye(len(UPOS))
    deprel_eye = torch.eye(len(DEPREL))
    with torch.no_grad():
        upos_emb = nn.Embedding(*upos_eye.shape)
        upos_emb.weight.data.copy_(upos_eye)
        upos_emb = upos_emb.to(device)

        deprel_emb = nn.Embedding(*deprel_eye.shape)
        deprel_emb.weight.data.copy_(deprel_eye)
        deprel_emb.to(device)

    for lang, d in test_data:
        with torch.no_grad():
            b_test = torch.from_numpy(d[:max_data, 0, :].astype("int32")).squeeze().to(device).long()
            # TODO move the testing part to CPU so it takes less memory in the GPU and can keep training while testing
            # doing operations in boolean form so it takes less space in gpu
            b_upos = torch.from_numpy(d[:max_data, 1, :].astype("bool")).squeeze().to(device).long()
            b_deprel = torch.from_numpy(d[:max_data, 2, :].astype("bool")).squeeze().to(device).long()
            _, _, _, dec = model(b_test)
            #         last_latent = latent[-1]
            upos, deprel = dec
            ones = torch.ones(1).to(device)
            zeros = torch.zeros(1).to(device)
            upos = torch.where(upos > 0.9, ones, zeros).bool().to(device)
            deprel = torch.where(deprel > 0.9, ones, zeros).bool().to(device)
            upos = upos.view([-1, 18])
            deprel = deprel.view([-1, 278])

        # FIXME this accuracy measurement does not work.
        upos_acc = (upos == upos_emb(b_upos).view([-1, 18])).sum().item() / upos.shape[0]
        deprel_acc = (deprel == deprel_emb(b_deprel).view([-1, 278])).sum().item() / deprel.shape[0]
        acc = (upos_acc + deprel_acc) / 2
        # print("accuracy : ", acc, upos_acc, deprel_acc)
        writer.add_scalar("LangAccuracy/test/" + lang, acc, global_step=epoch)
        writer.add_scalar("LangAccuracy/test/upos/" + lang, upos_acc, global_step=epoch)
        writer.add_scalar("LangAccuracy/test/deprel/" + lang, deprel_acc, global_step=epoch)

        del b_test
        del b_upos
        del b_deprel
        torch.cuda.empty_cache()
    epoch_acc /= len(test_data)  # although this is not faire as different languages give different results
    writer.add_scalar("EpochLangAccuracy/test/", epoch_acc, global_step=epoch)
    print('epoch: {}====> Test Accuracy set loss: {:.4f}'.format(epoch, acc))
    pass


def load_test_data(base_dir, max_samples=-1, max_seq_len=-1):
    """finds all ud-treebank data that was pre-processed and saved in numpy and loads it.
    Each file is loaded and kept in a tuple (lang, dataset) and returns a list of those values
    if max_samples or max_seq_len are set to a nunmber greater than zero these will limit the data returned
    """
    # load testing data ALL the training data

    # get all file paths for testing
    all_fnames = get_all_files_recurse(base_dir)
    fnames = [f for f in all_fnames if "test-charse" in f and f.endswith(".npy")]
    # load all test files
    test_data = []
    for f in fnames:
        data = np.load(f)
        # data is shape: [total samples, data channels (3), 1024]
        # print("data loading shape: ", data.shape)
        if max_seq_len > 0:
            data = data[:, :, :max_seq_len]
        if max_samples > 0:
            data = data[:max_samples, :, :]
        lang_name = path_leaf(f).split("-ud")[0]
        test_data.append((lang_name, data))
    return test_data

