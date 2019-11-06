import numpy as np
import torch
import torch.nn as nn
# good that PyTorch v1.3.0+ has Transformers already implemented
# from torch.nn.modules.transformer import Transformer
from torch.nn.modules.transformer import TransformerEncoder, TransformerEncoderLayer
# from torch.nn.modules.transformer import TransformerDecoder, TransformerDecoderLayer
import torch.nn.functional as F
import faiss


def _get_activation_fn(activation):
    if activation == "sigmoid":
        return F.sigmoid
    elif activation == "tanh":
        return F.tanh
    elif activation == "relu":
        return F.relu
    elif activation == "gelu":
        return F.gelu
    else:
        return None
        # raise RuntimeError("activation should be sigmoid/tanh/relu/gelu, not %s." % activation)


class UTF8Code(nn.Module):
    def __init__(self, utf8codebook, idx2char, char2idx):
        """

        :param utf8codebook:
        :param idx2char:
        :param char2idx:
        """
        # self._codebook = utf8codebook
        self._index = faiss.IndexFlatL2(utf8codebook)
        # faiss.IndexBin
        # self._index = faiss.GpuIndexFlatL2(utf8codebook)
        # faiss.IVFL
        self._idx2char = idx2char
        self._char2idx = char2idx

    def idx2char(self, idxs):
        pass

    def char2idx(self, chars):
        pass

    def embed2idx(self, embeds):
        pass

    def forward(self, x):
        # this function basically calls embed2idx
        # x should be (batch size, sequence width, embedding)
        return self.embed2idx(x)


class UTF8Embedding(nn.Module):
    def __init__(self, utf8codebook, transpose=False, lin_layers=(512, 512, 32),
                 activation=None, dropout=0.1):
        """
        Embedding Layer for UTF-8 based on pre-computed weights

        If the linear layers are created the output size of the embedding is the one of the last linear layer.
        By default there are no linear layers and the output of the embedding is the one given as input

        :param utf8codebook: Codebook containing the entire index to code matrix
        :param transpose: if the output should be transposed such as if True the result is shape
                          (batch size, embedding, sequence width) if False (default) shape is
                          (batch size, sequence width, embedding)
        :param lin_layers: iterable with size of the output of each linear layer, examples: [64], [256,64]
        :param activation: activation type
        :param dropout: for training dropout
        """
        self._transpose = transpose
        super(UTF8Embedding, self).__init__()
        codebook_shape = utf8codebook.shape
        self.embeds = nn.Embedding(*codebook_shape)
        self.embeds.weight.data.copy_(torch.from_numpy(utf8codebook))
        self.embeds.weight.requires_grad_(False)  # input embedding is fixed
        self.lin = nn.ModuleList()
        self.embedding = nn.ModuleList()
        if lin_layers:
            prev_dim = codebook_shape[1]
            for dim in lin_layers:
                lin = nn.Linear(prev_dim, dim)
                dropout = nn.Dropout(dropout)
                prev_dim = dim
                self.lin.extend([lin, dropout])

        self.embedding.append(self.embeds)
        self.embedding.extend(self.lin)
        self.activation = _get_activation_fn(activation)

    @property
    def transpose(self):
        return self._transpose

    @property
    def transpose(self, transpose):
        self._transpose = transpose

    def forward(self, x):
        # (batch size, sequence-width)
        ret = self.embedding(x)  # (batch size, sequence width[values]) -> # (batch size, sequence width, embedding)
        if self._transpose:
            ret = ret.transpose(1, 2)  # (batch size, embedding, sequence width)
        if self.activation:
            ret = self.activation(ret)
        return ret


class UTF8AttentionalEmbedding(UTF8Embedding):
    def __init__(self, utf8codebook, layers=2, nheads=4, ffdim=1024, outdim=32):
        """
        Embedding Layer for UTF-8 based on pre-computed weights, it adds attentional and linear layers
        to be able to modify the embedding dimension.
        This class can be used later frozen to recompute the embeddings and use UTF8Embedding directly
        :param utf8codebook: Codebook containing the entire index to code matrix
        :param layers: number of attention layers (channel-wise, for each character)
        :param nheads: number of heads of the attentional layers
        :param ffdim: feed forward dimension of the attentional linear layer
        :param outdim: output dimension of the code
        """

        # dimension would be:
        # 1024,
        super(UTF8AttentionalEmbedding, self).__init__(utf8codebook)
        # compute the input weights
        codebook_shape = self.embeds.weight.shape
        # shape is [len codebook, dim embedding=64]
        # assert is here so if code changes I know (this should be changed in the future)
        assert codebook_shape[1] == 64
        self.att = nn.ModuleList()
        for i in range(layers):
            att = TransformerEncoderLayer(codebook_shape[1], nheads, dim_feedforward=ffdim)
            self.att.append(att)

        self.lin = nn.Linear(codebook_shape[1], outdim)
        self.fw = nn.ModuleList()
        # pre-coded / pre-computed embeddings to (normally dim 32)
        self.fw.append(self.embeds)
        self.fw.extend(self.att)
        self.fw.append(self.lin)

    def forward(self, x):
        # (batch size, values)
        ret = self.fw(x)  # (batch size, sequence width[values]) -> # (batch size, sequence width, embedding)
        if self._transpose:
            ret = ret.transpose(1, 2)  # (batch size, embedding, sequence width)
        return ret


class UTF8Decoder(nn.Module):
    def __init__(self, utf8codebook_shape, lin_layers=(32, 512, 512), activation="gelu",
                 seg_indices=(0, 4, 256+4, 64+256+4, 2*64 + 256+4, 3*64 + 256+4), dropout=0.1):
        """
        This modules decodes a given code, if the code_dim is given (an iterable not None) then a MLP decoder is created to adapt
        from the input dimension to the utf8codebook dimension
        :param utf8codebook_shape:  shape of the pre-computed codebook used
        :param lin_layers: if not None, creates a MLP with the given dimensions  for example[512, 512, 388] and after that
        a multiple Softmax is used for each segment on the code
        :param activation: intermediate activation function
        :param seg_indices: START and END indices of the segments on the multihot code, default for 4 segments code
        for 1 segment1 is: seg_indices=(0, 4, 256+4)
        for 2 segments is: seg_indices=(0, 4, 256+4, 64+256+4)
        for 3 segments is: seg_indices=(0, 4, 256+4, 64+256+4, 2*64 + 256+4)
        for 4 segments is: seg_indices=(0, 4, 256+4, 64+256+4, 2*64 + 256+4, 3*64 + 256+4)
        but with the default value works for every segment encoding as it has a verification, so don't touch
        """
        super(UTF8Decoder, self).__init__()
        # self.embeds = nn.Embedding(*utf8codebook.shape)
        # self.embeds.weight.data.copy_(torch.from_numpy(utf8codebook))
        # self.embeds.weight.requires_grad(False)
        # the decoder has to pass from a dimension that compresses all the information to several vectors that
        # each encode one part of the output that later will be mapped by the utf8codebook to an int
        # the mechanism is:
        # input -> adaptation to utf8codebook_shape by linear layers ->
        #       -> separation in segments ->
        #       -> Softmax ->
        #       -> concatenation
        # And later must be decoded to an integer index. This decoding to int
        # will be done in another class as it's a work on it's own and better separate it

        # Linear layers to adapt input latent to the codebook dimension
        self.lin = nn.ModuleList()
        if lin_layers:
            lin_layers = list(lin_layers) + [utf8codebook_shape[1]]  # add dimension
            for i in range(len(lin_layers)-1):
                lin = nn.Linear(lin_layers[i], lin_layers[i+1])
                drop = nn.Dropout(dropout)
                self.lin.extend([lin, drop])
        self.activation = _get_activation_fn(activation)
        # Linear layers to adapt dimension for the separated softmax
        #  precomputing dimensions and filtering
        sidx = np.array(seg_indices)
        sidx = sidx[sidx <= utf8codebook_shape[1]]  # filter out unused segments (dimension)
        ridx = np.roll(sidx, shift=-1)  # get the end index of each segment
        dims = ridx - sidx   # get segment dimensions
        dims = dims[0 < dims]  # this takes out the remaining problem dimensions (mainly 0)
        # creating the separated pre-softmax linear layers
        self.segments = nn.ModuleList()  # the order is important as it is the concatenation order
        for d in dims:
            lin = nn.Linear(utf8codebook_shape[1], d)
            sm = nn.Softmax()  # softmax for this part of the code, the code has S+1 parts where S is the segment count
            self.segments.extend([lin, sm])

    def forward(self, x):
        # assumes input shape as:  (batch size, sequence width, embedding)
        # adapt input dimension to codebook
        ret = self.lin(x)
        if self.activation:
            ret = self.activation(ret)
        # process every segment part of the multihot-encoding
        segments = []
        for net in self.segments:
            segments.append(net(ret))
        # concatenate results in axis to get to the embedding size:
        res = torch.cat(segments, dim=-1)
        return res


class EncodeDecodeTest(nn.Module):
    pass


def main():
    pass
