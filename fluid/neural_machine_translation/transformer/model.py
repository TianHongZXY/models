from functools import partial
import numpy as np

import paddle.fluid as fluid
import paddle.fluid.layers as layers

from config import TrainTaskConfig, pos_enc_param_names, \
    encoder_input_data_names, decoder_input_data_names, label_data_names

# FIXME(guosheng): Remove out the batch_size from the model.
batch_size = TrainTaskConfig.batch_size


def position_encoding_init(n_position, d_pos_vec):
    """
    Generate the initial values for the sinusoid position encoding table.
    """
    position_enc = np.array([[
        pos / np.power(10000, 2 * (j // 2) / d_pos_vec)
        for j in range(d_pos_vec)
    ] if pos != 0 else np.zeros(d_pos_vec) for pos in range(n_position)])
    position_enc[1:, 0::2] = np.sin(position_enc[1:, 0::2])  # dim 2i
    position_enc[1:, 1::2] = np.cos(position_enc[1:, 1::2])  # dim 2i+1
    return position_enc.astype("float32")


def multi_head_attention(queries,
                         keys,
                         values,
                         attn_bias,
                         d_key,
                         d_value,
                         d_model,
                         n_head=1,
                         dropout_rate=0.):
    """
    Multi-Head Attention. Note that attn_bias is added to the logit before
    computing softmax activiation to mask certain selected positions so that
    they will not considered in attention weights.
    """
    if not (len(queries.shape) == len(keys.shape) == len(values.shape) == 3):
        raise ValueError(
            "Inputs: quries, keys and values should all be 3-D tensors.")

    def __compute_qkv(queries, keys, values, n_head, d_key, d_value):
        """
        Add linear projection to queries, keys, and values.
        """
        q = layers.fc(input=queries,
                      size=d_key * n_head,
                      param_attr=fluid.initializer.Xavier(
                          uniform=False,
                          fan_in=d_model * d_key,
                          fan_out=n_head * d_key),
                      bias_attr=False,
                      num_flatten_dims=2)
        k = layers.fc(input=keys,
                      size=d_key * n_head,
                      param_attr=fluid.initializer.Xavier(
                          uniform=False,
                          fan_in=d_model * d_key,
                          fan_out=n_head * d_key),
                      bias_attr=False,
                      num_flatten_dims=2)
        v = layers.fc(input=values,
                      size=d_value * n_head,
                      param_attr=fluid.initializer.Xavier(
                          uniform=False,
                          fan_in=d_model * d_value,
                          fan_out=n_head * d_value),
                      bias_attr=False,
                      num_flatten_dims=2)
        return q, k, v

    def __split_heads(x, n_head):
        """
        Reshape the last dimension of inpunt tensor x so that it becomes two
        dimensions and then transpose. Specifically, input a tensor with shape
        [bs, max_sequence_length, n_head * hidden_dim] then output a tensor
        with shape [bs, n_head, max_sequence_length, hidden_dim].
        """
        if n_head == 1:
            return x

        hidden_size = x.shape[-1]
        # FIXME(guosheng): Decouple the program desc with batch_size.
        reshaped = layers.reshape(
            x=x, shape=[batch_size, -1, n_head, hidden_size // n_head])

        # permuate the dimensions into:
        # [batch_size, n_head, max_sequence_len, hidden_size_per_head]
        return layers.transpose(x=reshaped, perm=[0, 2, 1, 3])

    def __combine_heads(x):
        """
        Transpose and then reshape the last two dimensions of inpunt tensor x
        so that it becomes one dimension, which is reverse to __split_heads.
        """
        if len(x.shape) == 3: return x
        if len(x.shape) != 4:
            raise ValueError("Input(x) should be a 4-D Tensor.")

        trans_x = layers.transpose(x, perm=[0, 2, 1, 3])
        # FIXME(guosheng): Decouple the program desc with batch_size.
        return layers.reshape(
            x=trans_x,
            shape=map(int,
                      [batch_size, -1, trans_x.shape[2] * trans_x.shape[3]]))

    def scaled_dot_product_attention(q, k, v, attn_bias, d_model, dropout_rate):
        """
        Scaled Dot-Product Attention
        """

        # FIXME(guosheng): Optimize the shape in reshape_op or softmax_op.

        # The current implementation of softmax_op only supports 2D tensor,
        # consequently it cannot be directly used here.
        # If to use the reshape_op, Besides, the shape of product inferred in
        # compile-time is not the actual shape in run-time. It cann't be used
        # to set the attribute of reshape_op.
        # So, here define the softmax for temporary solution.

        def __softmax(x, eps=1e-9):
            exp_out = layers.exp(x=x)
            sum_out = layers.reduce_sum(exp_out, dim=-1, keep_dim=False)
            return layers.elementwise_div(x=exp_out, y=sum_out, axis=0)

        scaled_q = layers.scale(x=q, scale=d_model**-0.5)
        product = layers.matmul(x=scaled_q, y=k, transpose_y=True)
        weights = __softmax(
            layers.elementwise_add(
                x=product, y=attn_bias) if attn_bias else product)
        if dropout_rate:
            weights = layers.dropout(
                weights, dropout_prob=dropout_rate, is_test=False)
        out = layers.matmul(weights, v)
        return out

    q, k, v = __compute_qkv(queries, keys, values, n_head, d_key, d_value)

    q = __split_heads(q, n_head)
    k = __split_heads(k, n_head)
    v = __split_heads(v, n_head)

    ctx_multiheads = scaled_dot_product_attention(q, k, v, attn_bias, d_model,
                                                  dropout_rate)

    out = __combine_heads(ctx_multiheads)

    # Project back to the model size.
    proj_out = layers.fc(input=out,
                         size=d_model,
                         param_attr=fluid.initializer.Xavier(uniform=False),
                         bias_attr=False,
                         num_flatten_dims=2)
    return proj_out


def positionwise_feed_forward(x, d_inner_hid, d_hid):
    """
    Position-wise Feed-Forward Networks.
    This module consists of two linear transformations with a ReLU activation
    in between, which is applied to each position separately and identically.
    """
    hidden = layers.fc(input=x,
                       size=d_inner_hid,
                       num_flatten_dims=2,
                       param_attr=fluid.initializer.Uniform(
                           low=-(d_hid**-0.5), high=(d_hid**-0.5)),
                       act="relu")
    out = layers.fc(input=hidden,
                    size=d_hid,
                    num_flatten_dims=2,
                    param_attr=fluid.initializer.Uniform(
                        low=-(d_inner_hid**-0.5), high=(d_inner_hid**-0.5)))
    return out


def pre_post_process_layer(prev_out, out, process_cmd, dropout=0.):
    """
    Add residual connection, layer normalization and droput to the out tensor
    optionally according to the value of process_cmd.

    This will be used before or after multi-head attention and position-wise
    feed-forward networks.
    """
    for cmd in process_cmd:
        if cmd == "a":  # add residual connection
            out = out + prev_out if prev_out else out
        elif cmd == "n":  # add layer normalization
            out = layers.layer_norm(
                out,
                begin_norm_axis=len(out.shape) - 1,
                param_attr=fluid.initializer.Constant(1.),
                bias_attr=fluid.initializer.Constant(0.))
        elif cmd == "d":  # add dropout
            if dropout:
                out = layers.dropout(out, dropout_prob=dropout, is_test=False)
    return out


pre_process_layer = partial(pre_post_process_layer, None)
post_process_layer = pre_post_process_layer


def prepare_encoder(src_word,
                    src_pos,
                    src_vocab_size,
                    src_emb_dim,
                    src_pad_idx,
                    src_max_len,
                    dropout=0.,
                    pos_pad_idx=0,
                    pos_enc_param_name=None):
    """Add word embeddings and position encodings.
    The output tensor has a shape of:
    [batch_size, max_src_length_in_batch, d_model].

    This module is used at the bottom of the encoder stacks.
    """
    src_word_emb = layers.embedding(
        src_word,
        size=[src_vocab_size, src_emb_dim],
        padding_idx=src_pad_idx,
        param_attr=fluid.initializer.Normal(0., 1.))
    src_pos_enc = layers.embedding(
        src_pos,
        size=[src_max_len, src_emb_dim],
        padding_idx=pos_pad_idx,
        param_attr=fluid.ParamAttr(
            name=pos_enc_param_name, trainable=False))
    enc_input = src_word_emb + src_pos_enc

    # FIXME(guosheng): Decouple the program desc with batch_size.
    enc_input = layers.reshape(x=enc_input, shape=[batch_size, -1, src_emb_dim])
    return layers.dropout(
        enc_input, dropout_prob=dropout,
        is_test=False) if dropout else enc_input


prepare_encoder = partial(
    prepare_encoder, pos_enc_param_name=pos_enc_param_names[0])
prepare_decoder = partial(
    prepare_encoder, pos_enc_param_name=pos_enc_param_names[1])


def encoder_layer(enc_input,
                  attn_bias,
                  n_head,
                  d_key,
                  d_value,
                  d_model,
                  d_inner_hid,
                  dropout_rate=0.):
    """The encoder layers that can be stacked to form a deep encoder.

    This module consits of a multi-head (self) attention followed by
    position-wise feed-forward networks and both the two components companied
    with the post_process_layer to add residual connection, layer normalization
    and droput.
    """
    attn_output = multi_head_attention(enc_input, enc_input, enc_input,
                                       attn_bias, d_key, d_value, d_model,
                                       n_head, dropout_rate)
    attn_output = post_process_layer(enc_input, attn_output, "dan",
                                     dropout_rate)
    ffd_output = positionwise_feed_forward(attn_output, d_inner_hid, d_model)
    return post_process_layer(attn_output, ffd_output, "dan", dropout_rate)


def encoder(enc_input,
            attn_bias,
            n_layer,
            n_head,
            d_key,
            d_value,
            d_model,
            d_inner_hid,
            dropout_rate=0.):
    """
    The encoder is composed of a stack of identical layers returned by calling
    encoder_layer.
    """
    for i in range(n_layer):
        enc_output = encoder_layer(enc_input, attn_bias, n_head, d_key, d_value,
                                   d_model, d_inner_hid, dropout_rate)
        enc_input = enc_output
    return enc_output


def decoder_layer(dec_input,
                  enc_output,
                  slf_attn_bias,
                  dec_enc_attn_bias,
                  n_head,
                  d_key,
                  d_value,
                  d_model,
                  d_inner_hid,
                  dropout_rate=0.):
    """ The layer to be stacked in decoder part.

    The structure of this module is similar to that in the encoder part except
    a multi-head attention is added to implement encoder-decoder attention.
    """
    slf_attn_output = multi_head_attention(
        dec_input,
        dec_input,
        dec_input,
        slf_attn_bias,
        d_key,
        d_value,
        d_model,
        n_head,
        dropout_rate, )
    slf_attn_output = post_process_layer(
        dec_input,
        slf_attn_output,
        "dan",  # residual connection + dropout + layer normalization
        dropout_rate, )
    enc_attn_output = multi_head_attention(
        slf_attn_output,
        enc_output,
        enc_output,
        dec_enc_attn_bias,
        d_key,
        d_value,
        d_model,
        n_head,
        dropout_rate, )
    enc_attn_output = post_process_layer(
        slf_attn_output,
        enc_attn_output,
        "dan",  # residual connection + dropout + layer normalization
        dropout_rate, )
    ffd_output = positionwise_feed_forward(
        enc_attn_output,
        d_inner_hid,
        d_model, )
    dec_output = post_process_layer(
        enc_attn_output,
        ffd_output,
        "dan",  # residual connection + dropout + layer normalization
        dropout_rate, )
    return dec_output


def decoder(dec_input,
            enc_output,
            dec_slf_attn_bias,
            dec_enc_attn_bias,
            n_layer,
            n_head,
            d_key,
            d_value,
            d_model,
            d_inner_hid,
            dropout_rate=0.):
    """
    The decoder is composed of a stack of identical decoder_layer layers.
    """
    for i in range(n_layer):
        dec_output = decoder_layer(
            dec_input,
            enc_output,
            dec_slf_attn_bias,
            dec_enc_attn_bias,
            n_head,
            d_key,
            d_value,
            d_model,
            d_inner_hid,
            dropout_rate, )
        dec_input = dec_output
    return dec_output


def make_inputs(input_data_names,
                n_head,
                d_model,
                batch_size,
                max_length,
                slf_attn_bias_flag,
                src_attn_bias_flag,
                pos_flag=1):
    """
    Define the input data layers for the transformer model.
    """
    input_layers = []
    # The shapes here act as placeholder.
    # The shapes set here is to pass the infer-shape in compile time.
    word = layers.data(
        name=input_data_names[0],
        shape=[batch_size * max_length, 1],
        dtype="int64",
        append_batch_size=False)
    input_layers += [word]
    # This is used for position data or label weight.
    pos = layers.data(
        name=input_data_names[1],
        shape=[batch_size * max_length, 1],
        dtype="int64" if pos_flag else "float32",
        append_batch_size=False)
    input_layers += [pos]
    if slf_attn_bias_flag:
        # This is used for attention bias or encoder output.
        slf_attn_bias = layers.data(
            name=input_data_names[2]
            if slf_attn_bias_flag == 1 else input_data_names[-1],
            shape=[batch_size, n_head, max_length, max_length]
            if slf_attn_bias_flag == 1 else [batch_size, max_length, d_model],
            dtype="float32",
            append_batch_size=False)
        input_layers += [slf_attn_bias]
    if src_attn_bias_flag:
        src_attn_bias = layers.data(
            name=input_data_names[3],
            shape=[batch_size, n_head, max_length, max_length],
            dtype="float32",
            append_batch_size=False)
        input_layers += [src_attn_bias]
    return input_layers


def transformer(
        src_vocab_size,
        trg_vocab_size,
        max_length,
        n_layer,
        n_head,
        d_key,
        d_value,
        d_model,
        d_inner_hid,
        dropout_rate,
        src_pad_idx,
        trg_pad_idx,
        pos_pad_idx, ):
    enc_input_layers = make_inputs(encoder_input_data_names, n_head, d_model,
                                   batch_size, max_length, 1, 0)

    enc_output = wrap_encoder(
        src_vocab_size,
        max_length,
        n_layer,
        n_head,
        d_key,
        d_value,
        d_model,
        d_inner_hid,
        dropout_rate,
        src_pad_idx,
        pos_pad_idx,
        enc_input_layers, )

    dec_input_layers = make_inputs(decoder_input_data_names, n_head, d_model,
                                   batch_size, max_length, 1, 1)

    predict = wrap_decoder(
        trg_vocab_size,
        max_length,
        n_layer,
        n_head,
        d_key,
        d_value,
        d_model,
        d_inner_hid,
        dropout_rate,
        trg_pad_idx,
        pos_pad_idx,
        dec_input_layers,
        enc_output, )

    # Padding index do not contribute to the total loss. The weights is used to
    # cancel padding index in calculating the loss.
    gold, weights = make_inputs(label_data_names, n_head, d_model, batch_size,
                                max_length, 0, 0, 0)
    cost = layers.cross_entropy(input=predict, label=gold)
    weighted_cost = cost * weights
    return layers.reduce_sum(weighted_cost), predict


def wrap_encoder(src_vocab_size,
                 max_length,
                 n_layer,
                 n_head,
                 d_key,
                 d_value,
                 d_model,
                 d_inner_hid,
                 dropout_rate,
                 src_pad_idx,
                 pos_pad_idx,
                 enc_input_layers=None):
    """
    The wrapper assembles together all needed layers for the encoder.
    """
    if enc_input_layers is None:
        # This is used to implement independent encoder program in inference.
        src_word, src_pos, src_slf_attn_bias = make_inputs(
            encoder_input_data_names, n_head, d_model, batch_size, max_length,
            True, False)
    else:
        src_word, src_pos, src_slf_attn_bias = enc_input_layers
    enc_input = prepare_encoder(
        src_word,
        src_pos,
        src_vocab_size,
        d_model,
        src_pad_idx,
        max_length,
        dropout_rate, )
    enc_output = encoder(
        enc_input,
        src_slf_attn_bias,
        n_layer,
        n_head,
        d_key,
        d_value,
        d_model,
        d_inner_hid,
        dropout_rate, )
    return enc_output


def wrap_decoder(trg_vocab_size,
                 max_length,
                 n_layer,
                 n_head,
                 d_key,
                 d_value,
                 d_model,
                 d_inner_hid,
                 dropout_rate,
                 trg_pad_idx,
                 pos_pad_idx,
                 dec_input_layers=None,
                 enc_output=None):
    """
    The wrapper assembles together all needed layers for the decoder.
    """
    if dec_input_layers is None:
        # This is used to implement independent decoder program in inference.
        # No need for trg_slf_attn_bias because of no paddings in inference.
        trg_word, trg_pos, enc_output, trg_src_attn_bias = make_inputs(
            decoder_input_data_names, n_head, d_model, batch_size, max_length,
            2, 1)
        trg_slf_attn_bias = None
    else:
        trg_word, trg_pos, trg_slf_attn_bias, trg_src_attn_bias = dec_input_layers

    dec_input = prepare_decoder(
        trg_word,
        trg_pos,
        trg_vocab_size,
        d_model,
        trg_pad_idx,
        max_length,
        dropout_rate, )
    dec_output = decoder(
        dec_input,
        enc_output,
        trg_slf_attn_bias,
        trg_src_attn_bias,
        n_layer,
        n_head,
        d_key,
        d_value,
        d_model,
        d_inner_hid,
        dropout_rate, )

    predict = layers.reshape(
        x=layers.fc(input=dec_output,
                    size=trg_vocab_size,
                    bias_attr=False,
                    num_flatten_dims=2),
        shape=[-1, trg_vocab_size],
        act="softmax")
    return predict
