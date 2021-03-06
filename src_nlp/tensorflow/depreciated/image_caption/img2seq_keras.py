from tensorflow.python.layers import core as core_layers
import tensorflow as tf
import numpy as np


class Image2Seq:
    def __init__(self, img_size, word2idx, img_ch=3,
                 embedding_dim=256, rnn_size=256, n_layers=2, grad_clip=5.,
                 force_teaching_ratio=0.8, beam_width=5,
                 sess=tf.Session()):
        self.img_size = img_size
        self.img_ch = img_ch
        self.word2idx = word2idx
        self.embedding_dim = embedding_dim

        self.rnn_size = rnn_size
        self.n_layers = n_layers
        self.grad_clip = grad_clip
        self.force_teaching_ratio = force_teaching_ratio
        self.beam_width = beam_width

        self.sess = sess
        self._pointer = None
        self._img_size = self.img_size
        self._n_filter = None
        self.build_graph()
    # end constructor


    def build_graph(self):
        self.add_input_layer()
        self.add_encoder()
        with tf.variable_scope('decode'):
            self.add_decoder_for_training()
        with tf.variable_scope('decode', reuse=True):
            self.add_decoder_for_inference()
        self.add_backward_path()
    # end method


    def add_input_layer(self):
        self.X = tf.placeholder(tf.float32, [None, self.img_ch, self.img_size[0], self.img_size[1]])
        self.Y = tf.placeholder(tf.int32, [None, None])
        self.Y_seq_len = tf.placeholder(tf.int32, [None])
        self.batch_size = tf.shape(self.X)[0]
        self._pointer = tf.transpose(self.X, [0, 2, 3, 1])
    # end method


    def add_encoder(self):
        cnn = tf.keras.applications.vgg16.VGG16(
            weights = 'imagenet',
            include_top = False,
            input_shape = (self.img_size[0], self.img_size[1], self.img_ch))
        for i, layer in enumerate(cnn.layers[:11]):
            print("%s: not trainable"%layer.name)
            layer.trainable = False
        for i, layer in enumerate(cnn.layers[11:]):
            print("%s: trainable"%layer.name)
            layer.trainable = True
        feature = cnn(self._pointer)
        feature_shape = cnn.layers[-1].output_shape
        self.encoder_out = tf.reshape(feature, [self.batch_size, np.prod(feature_shape[1:3]), feature_shape[-1]])
        flat = tf.contrib.layers.flatten(feature)
        proj = tf.layers.dense(flat, self.rnn_size)
        self.encoder_state = tuple([tf.nn.rnn_cell.LSTMStateTuple(c=proj, h=proj) for _ in range(self.n_layers)])
    # end method


    def add_decoder_for_training(self):
        self.add_attention_for_training()
        decoder_embedding = tf.get_variable('decoder_embedding', [len(self.word2idx), self.embedding_dim],
                                             tf.float32, tf.random_uniform_initializer(-1.0, 1.0))
        
        training_helper = tf.contrib.seq2seq.ScheduledEmbeddingTrainingHelper(
            inputs = tf.nn.embedding_lookup(decoder_embedding, self.processed_decoder_input()),
            sequence_length = self.Y_seq_len-1,
            embedding = decoder_embedding,
            sampling_probability = 1-self.force_teaching_ratio,
            time_major = False)
        training_decoder = tf.contrib.seq2seq.BasicDecoder(
            cell = self.decoder_cell,
            helper = training_helper,
            initial_state = self.decoder_cell.zero_state(self.batch_size, tf.float32).clone(cell_state=self.encoder_state),
            output_layer = core_layers.Dense(len(self.word2idx)))
        training_decoder_output, _, _ = tf.contrib.seq2seq.dynamic_decode(
            decoder = training_decoder,
            impute_finished = True,
            maximum_iterations = tf.reduce_max(self.Y_seq_len-1))
        self.training_logits = training_decoder_output.rnn_output
    # end method


    def add_decoder_for_inference(self):
        self.add_attention_for_inference()

        predicting_decoder = tf.contrib.seq2seq.BeamSearchDecoder(
            cell = self.decoder_cell,
            embedding = tf.get_variable('decoder_embedding'),
            start_tokens = tf.tile(tf.constant([self.word2idx['<start>']], dtype=tf.int32), [self.batch_size]),
            end_token = self.word2idx['<end>'],
            initial_state = self.decoder_cell.zero_state(self.batch_size * self.beam_width, tf.float32).clone(
                            cell_state = self.encoder_state_tiled),
            beam_width = self.beam_width,
            output_layer = core_layers.Dense(len(self.word2idx), _reuse=True),
            length_penalty_weight = 0.0)
        predicting_decoder_output, _, _ = tf.contrib.seq2seq.dynamic_decode(
            decoder = predicting_decoder,
            impute_finished = False,
            maximum_iterations = 2 * tf.reduce_max(self.Y_seq_len-1))
        self.predicting_ids = predicting_decoder_output.predicted_ids[:, :, 0]
    # end method


    def add_backward_path(self):
        masks = tf.sequence_mask(self.Y_seq_len-1, tf.reduce_max(self.Y_seq_len-1), dtype=tf.float32)
        self.loss = tf.contrib.seq2seq.sequence_loss(
            logits = self.training_logits, targets = self.processed_decoder_output(), weights = masks)
        
        params = tf.trainable_variables()
        gradients = tf.gradients(self.loss, params)
        clipped_gradients, _ = tf.clip_by_global_norm(gradients, self.grad_clip)
        self.train_op = tf.train.AdamOptimizer().apply_gradients(zip(clipped_gradients, params))
    # end method


    def lstm_cell(self, reuse=False):
        return tf.nn.rnn_cell.LSTMCell(self.rnn_size, initializer=tf.orthogonal_initializer(), reuse=reuse)
    # end method

    def processed_decoder_input(self):
        return tf.strided_slice(self.Y, [0, 0], [self.batch_size, -1], [1, 1]) # remove last char
    # end method

    def processed_decoder_output(self):
        return tf.strided_slice(self.Y, [0, 1], [self.batch_size, tf.shape(self.Y)[1]], [1, 1]) # remove first char
    # end method


    def add_attention_for_training(self):
        attention_mechanism = tf.contrib.seq2seq.LuongAttention(
            num_units = self.rnn_size, 
            memory = self.encoder_out)
        self.decoder_cell = tf.contrib.seq2seq.AttentionWrapper(
            cell = tf.nn.rnn_cell.MultiRNNCell([self.lstm_cell() for _ in range(self.n_layers)]),
            attention_mechanism = attention_mechanism,
            attention_layer_size = self.rnn_size)
    # end method


    def add_attention_for_inference(self):
        self.encoder_out_tiled = tf.contrib.seq2seq.tile_batch(self.encoder_out, self.beam_width)
        self.encoder_state_tiled = tf.contrib.seq2seq.tile_batch(self.encoder_state, self.beam_width)

        attention_mechanism = tf.contrib.seq2seq.LuongAttention(
            num_units = self.rnn_size, 
            memory = self.encoder_out_tiled)
        self.decoder_cell = tf.contrib.seq2seq.AttentionWrapper(
            cell = tf.nn.rnn_cell.MultiRNNCell([self.lstm_cell(reuse=True) for _ in range(self.n_layers)]),
            attention_mechanism = attention_mechanism,
            attention_layer_size = self.rnn_size)
    # end method


    def partial_fit(self, images, captions, lengths):
        _, loss = self.sess.run([self.train_op, self.loss],
            {self.X:images, self.Y:captions, self.Y_seq_len:lengths})
        return loss
    # end method


    def infer(self, image, idx2word):
        idx2word[-1] = '-1'
        out_indices = self.sess.run(self.predicting_ids,
            {self.X:image, self.Y_seq_len:[20]})[0]
        print('{}'.format(' '.join([idx2word[i] for i in out_indices])))
    # end method
# end class
