import torch
import torch.nn as nn
import torch.nn.functional as F
import math
from args import args

Dword = args.glove_dim
Dchar = args.char_dim
D = args.d_model
dropout = args.qanet_dropout
dropout_char = args.qanet_char_dropout


def mask_logits(inputs, mask):
    mask = mask.type(torch.float32)
    return inputs + (-1e30) * (1 - mask)


class Initialized_Conv1d(nn.Module):
    def __init__(self, in_channels, out_channels, kernel_size=1, relu=False, 
                 stride=1, padding=0, groups=1, bias=False):
        super().__init__()
        self.out = nn.Conv1d(
            in_channels, out_channels, kernel_size, 
            stride=stride, padding=padding, groups=groups, bias=bias)
        if relu is True:
            self.relu = True
            nn.init.kaiming_normal_(self.out.weight, nonlinearity='relu')
        else:
            self.relu = False
            nn.init.xavier_uniform_(self.out.weight)

    def forward(self, x):
        if self.relu == True:
            return F.relu(self.out(x))
        else:
            return self.out(x)


def PosEncoder(x, min_timescale=1.0, max_timescale=1.0e4):
    x = x.transpose(1, 2)
    length, channels = x.shape[1], x.shape[2]
    signal = get_timing_signal(length, channels, min_timescale, max_timescale)
    if torch.cuda.is_available():
        signal = signal.cuda()
    return (x + signal).transpose(1,2)


def get_timing_signal(length, channels, min_timescale=1.0, max_timescale=1.0e4):
    position = torch.arange(length).type(torch.float32)
    num_timescales = channels // 2
    log_timescale_increment = (
        math.log(float(max_timescale) / float(min_timescale)) / (float(num_timescales)-1)
    )
    inv_timescales = min_timescale * torch.exp(
        torch.arange(num_timescales).type(torch.float32) * -log_timescale_increment)
    scaled_time = position.unsqueeze(1) * inv_timescales.unsqueeze(0)
    signal = torch.cat([torch.sin(scaled_time), torch.cos(scaled_time)], dim = 1)
    m = nn.ZeroPad2d((0, (channels % 2), 0, 0))
    signal = m(signal)
    signal = signal.view(1, length, channels)
    return signal


class DepthwiseSeparableConv(nn.Module):
    def __init__(self, in_ch, out_ch, k, bias=True):
        super().__init__()
        self.depthwise_conv = nn.Conv1d(
            in_channels=in_ch, out_channels=in_ch, kernel_size=k, 
            groups=in_ch, padding=k // 2, bias=False)
        self.pointwise_conv = nn.Conv1d(in_channels=in_ch, out_channels=out_ch, 
                                        kernel_size=1, padding=0, bias=bias)
    def forward(self, x):
        return F.relu(self.pointwise_conv(self.depthwise_conv(x)))


class Highway(nn.Module):
    def __init__(self, layer_num: int, size=D):
        super().__init__()
        self.n = layer_num
        self.linear = nn.ModuleList([
            Initialized_Conv1d(size, size, relu=False, bias=True) 
            for _ in range(self.n)
        ])
        self.gate = nn.ModuleList([
            Initialized_Conv1d(size, size, bias=True) for _ in range(self.n)])

    def forward(self, x):
        #x: shape [batch_size, hidden_size, length]
        for i in range(self.n):
            gate = torch.sigmoid(self.gate[i](x))
            nonlinear = self.linear[i](x)
            nonlinear = F.dropout(nonlinear, p=dropout, training=self.training)
            x = gate * nonlinear + (1 - gate) * x
            #x = F.relu(x)
        return x


class SelfAttention(nn.Module):
    def __init__(self, n_head=8):
        super().__init__()
        self.n_head = n_head
        self.mem_conv = Initialized_Conv1d(
            D, D * 2, kernel_size=1, relu=False, bias=False)
        self.query_conv = Initialized_Conv1d(
            D, D, kernel_size=1, relu=False, bias=False)

        bias = torch.empty(1)
        nn.init.constant_(bias, 0)
        self.bias = nn.Parameter(bias)

    def forward(self, queries, mask):
        memory = queries

        memory = self.mem_conv(memory)
        query = self.query_conv(queries)
        memory = memory.transpose(1, 2)
        query = query.transpose(1, 2)
        Q = self.split_last_dim(query, self.n_head)
        K, V = [
            self.split_last_dim(tensor, self.n_head) 
            for tensor in torch.split(memory, D, dim=2)
        ]

        key_depth_per_head = D // self.n_head
        Q *= key_depth_per_head**-0.5
        x = self.dot_product_attention(Q, K, V, mask = mask)
        return self.combine_last_two_dim(x.permute(0,2,1,3)).transpose(1, 2)

    def dot_product_attention(self, q, k ,v, bias = False, mask = None):
        """dot-product attention.
        Args:
        q: a Tensor with shape [batch, heads, length_q, depth_k]
        k: a Tensor with shape [batch, heads, length_kv, depth_k]
        v: a Tensor with shape [batch, heads, length_kv, depth_v]
        bias: bias Tensor (see attention_bias())
        is_training: a bool of training
        scope: an optional string
        Returns:
        A Tensor.
        """
        logits = torch.matmul(q,k.permute(0,1,3,2))
        if bias:
            logits += self.bias
        if mask is not None:
            shapes = [x  if x != None else -1 for x in list(logits.size())]
            mask = mask.view(shapes[0], 1, 1, shapes[-1])
            logits = mask_logits(logits, mask)
        weights = F.softmax(logits, dim=-1)
        # dropping out the attention links for each of the heads
        weights = F.dropout(weights, p=dropout, training=self.training)
        return torch.matmul(weights, v)

    def split_last_dim(self, x, n):
        """Reshape x so that the last dimension becomes two dimensions.
        The first of these two dimensions is n.
        Args:
        x: a Tensor with shape [..., m]
        n: an integer.
        Returns:
        a Tensor with shape [..., n, m/n]
        """
        old_shape = list(x.size())
        last = old_shape[-1]
        new_shape = old_shape[:-1] + [n] + [last // n if last else None]
        ret = x.view(new_shape)
        return ret.permute(0, 2, 1, 3)

    def combine_last_two_dim(self, x):
        """Reshape x so that the last two dimension become one.
        Args:
        x: a Tensor with shape [..., a, b]
        Returns:
        a Tensor with shape [..., ab]
        """
        old_shape = list(x.size())
        a, b = old_shape[-2:]
        new_shape = old_shape[:-2] + [a * b if a and b else None]
        ret = x.contiguous().view(new_shape)
        return ret


class Embedding(nn.Module):
    def __init__(self):
        super().__init__()
        self.conv2d = nn.Conv2d(
            Dchar, D, kernel_size=(1,5), padding=0, bias=True)
        nn.init.kaiming_normal_(self.conv2d.weight, nonlinearity='relu')
        self.conv1d = Initialized_Conv1d(Dword+D, D, bias=False)
        self.high = Highway(2)

    def forward(self, ch_emb, wd_emb):
        ch_emb = ch_emb.permute(0, 3, 1, 2)
        ch_emb = F.dropout(ch_emb, p=dropout_char, training=self.training)
        ch_emb = self.conv2d(ch_emb)
        ch_emb = F.relu(ch_emb)
        ch_emb, _ = torch.max(ch_emb, dim=3)
        ch_emb = ch_emb.squeeze()

        wd_emb = F.dropout(wd_emb, p=dropout, training=self.training)
        wd_emb = wd_emb.transpose(1, 2)
        emb = torch.cat([ch_emb, wd_emb], dim=1)
        emb = self.conv1d(emb)
        emb = self.high(emb)
        return emb


class EncoderBlock(nn.Module):
    def __init__(self, conv_num: int, ch_num: int, k: int, n_head=8):
        super().__init__()
        self.convs = nn.ModuleList([
            DepthwiseSeparableConv(ch_num, ch_num, k) for _ in range(conv_num)
        ])
        self.self_att = SelfAttention(n_head)
        self.FFN_1 = Initialized_Conv1d(ch_num, ch_num, relu=True, bias=True)
        self.FFN_2 = Initialized_Conv1d(ch_num, ch_num, bias=True)
        self.norm_C = nn.ModuleList([nn.LayerNorm(D) for _ in range(conv_num)])
        self.norm_1 = nn.LayerNorm(D)
        self.norm_2 = nn.LayerNorm(D)
        self.conv_num = conv_num

    def forward(self, x, mask, l, blks):
        total_layers = (self.conv_num+1)*blks
        out = PosEncoder(x)
        for i, conv in enumerate(self.convs):
            res = out
            out = self.norm_C[i](out.transpose(1,2)).transpose(1,2)
            if (i) % 2 == 0:
                out = F.dropout(out, p=dropout, training=self.training)
            out = conv(out)
            out = self.layer_dropout(out, res, dropout*float(l)/total_layers)
            l += 1
        res = out
        out = self.norm_1(out.transpose(1,2)).transpose(1,2)
        out = F.dropout(out, p=dropout, training=self.training)
        out = self.self_att(out, mask)
        out = self.layer_dropout(out, res, dropout*float(l)/total_layers)
        l += 1
        res = out

        out = self.norm_2(out.transpose(1,2)).transpose(1,2)
        out = F.dropout(out, p=dropout, training=self.training)
        out = self.FFN_1(out)
        out = self.FFN_2(out)
        out = self.layer_dropout(out, res, dropout*float(l)/total_layers)
        return out

    def layer_dropout(self, inputs, residual, dropout):
        if self.training == True:
            pred = torch.empty(1).uniform_(0,1) < dropout
            if pred:
                return residual
            else:
                return F.dropout(inputs, dropout, 
                                 training=self.training) + residual
        else:
            return inputs + residual


class CQAttention(nn.Module):
    def __init__(self):
        super().__init__()
        w4C = torch.empty(D, 1)
        w4Q = torch.empty(D, 1)
        w4mlu = torch.empty(1, 1, D)
        nn.init.xavier_uniform_(w4C)
        nn.init.xavier_uniform_(w4Q)
        nn.init.xavier_uniform_(w4mlu)
        self.w4C = nn.Parameter(w4C)
        self.w4Q = nn.Parameter(w4Q)
        self.w4mlu = nn.Parameter(w4mlu)

        bias = torch.empty(1)
        nn.init.constant_(bias, 0)
        self.bias = nn.Parameter(bias)

    def forward(self, C, Q, Cmask, Qmask):
        C = C.transpose(1, 2) # after transpose (bs, ctxt_len, d_model)
        Q = Q.transpose(1, 2)# after transpose (bs, ques_len, d_model)
        batch_size = C.shape[0]
        Lc, Lq = C.shape[1], Q.shape[1]
        S = self.trilinear_for_attention(C, Q)
        Cmask = Cmask.view(batch_size, Lc, 1)
        Qmask = Qmask.view(batch_size, 1, Lq)
        S1 = F.softmax(mask_logits(S, Qmask), dim=2)
        S2 = F.softmax(mask_logits(S, Cmask), dim=1)
        # (bs, ctxt_len, ques_len) x (bs, ques_len, d_model) => (bs, ctxt_len, d_model)
        A = torch.bmm(S1, Q)
        # (bs, c_len, c_len) x (bs, c_len, hid_size) => (bs, c_len, hid_size)
        B = torch.bmm(torch.bmm(S1, S2.transpose(1, 2)), C)
        out = torch.cat([C, A, torch.mul(C, A), torch.mul(C, B)], dim=2) # (bs, ctxt_len, 4 * d_model)
        return out.transpose(1, 2)

    def trilinear_for_attention(self, C, Q):
        C = F.dropout(C, p=dropout, training=self.training)
        Q = F.dropout(Q, p=dropout, training=self.training)
        Lc, Lq = C.shape[1], Q.shape[1]
        subres0 = torch.matmul(C, self.w4C).expand([-1, -1, Lq])
        subres1 = torch.matmul(Q, self.w4Q).transpose(1, 2).expand([-1, Lc, -1])
        subres2 = torch.matmul(C * self.w4mlu, Q.transpose(1,2))
        # print('qanet_modules', subres0.shape, subres1.shape, subres2.shape)
        res = subres0 + subres1 + subres2
        res += self.bias
        return res


class Pointer(nn.Module):
    def __init__(self):
        super().__init__()
        self.w1 = Initialized_Conv1d(D*2, 1)
        self.w2 = Initialized_Conv1d(D*2, 1)

    def forward(self, M1, M2, M3, mask):
        X1 = torch.cat([M1, M2], dim=1)
        X2 = torch.cat([M1, M3], dim=1)
        Y1 = mask_logits(self.w1(X1).squeeze(), mask)
        Y2 = mask_logits(self.w2(X2).squeeze(), mask)
        # print(Y1[0])
        p1 = F.log_softmax(Y1, dim=1)
        p2 = F.log_softmax(Y2, dim=1)
        return p1, p2
