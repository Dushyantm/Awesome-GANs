import tensorflow as tf

import sys

sys.path.append('../')
import tfutil as t


tf.set_random_seed(777)  # reproducibility


class AdaMaxOptimizer:

    def __init__(self, learning_rate=1e-3, beta1=0.5, beta2=0.9, use_locking=True):

        """
        :param learning_rate: training learning rate, default 1e-3
        :param beta1: ada optimizer beta1 (momentum), default 0.5
        :param beta2: ada optimizer beta2 (momentum), default 0.9
        :param use_locking: using locking, default True
        """

        self.lr = tf.convert_to_tensor(learning_rate, name='lr')
        self.beta1 = tf.convert_to_tensor(beta1, name='beta1')
        self.beta2 = tf.convert_to_tensor(beta2, name='beta2')
        self.use_locking = tf.convert_to_tensor(use_locking, name='lock')


class MAGAN:

    def __init__(self, s, batch_size=64, height=28, width=28, channel=1, n_classes=10,
                 sample_num=10 * 10, sample_size=10,
                 n_input=784, df_dim=64, gf_dim=64, fc_unit=512,
                 z_dim=128, g_lr=2e-4, d_lr=2e-4):

        """
        # General Settings
        :param s: TF Session
        :param batch_size: training batch size, default 64
        :param height: input image height, default 28
        :param width: input image width, default 28
        :param channel: input image channel, default 1 (gray-scale)
        - in case of MNIST, image size is 28x28x1(HWC).
        :param n_classes: input dataset's classes
        - in case of MNIST, 10 (0 ~ 9)

        # Output Settings
        :param sample_num: the number of output images, default 100
        :param sample_size: sample image size, default 10

        # For CNN model
        :param n_input: input image size, default 784(28x28)
        :param df_dim: discriminator filter, default 64
        :param gf_dim: generator filter, default 64
        :param fc_unit: the number of fully connected filters, default 512

        # Training Option
        :param z_dim: z dimension (kinda noise), default 128
        :param g_lr: generator learning rate, default 2e-4
        :param d_lr: discriminator learning rate, default 2e-4
        """

        self.s = s
        self.batch_size = batch_size

        self.height = height
        self.width = width
        self.channel = channel
        self.image_shape = [self.batch_size, self.height, self.width, self.channel]
        self.n_classes = n_classes

        self.sample_num = sample_num
        self.sample_size = sample_size

        self.n_input = n_input
        self.df_dim = df_dim
        self.gf_dim = gf_dim
        self.fc_unit = fc_unit

        self.z_dim = z_dim
        self.beta1 = 0.5
        self.beta2 = 0.9
        self.d_lr, self.g_lr = d_lr, g_lr
        self.pt_lambda = 0.1

        # pre-defined
        self.g_loss = 0.
        self.d_loss = 0.
        self.d_real_loss = 0.
        self.d_fake_loss = 0.

        self.g = None
        self.g_test = None

        self.d_op = None
        self.d_real_op = None
        self.g_op = None

        self.merged = None
        self.writer = None
        self.saver = None

        # Placeholders
        self.x = tf.placeholder(tf.float32,
                                shape=[None, self.height, self.width, self.channel],
                                name="x-image")                                        # (-1, 28, 28, 1)
        self.z = tf.placeholder(tf.float32, shape=[None, self.z_dim], name='z-noise')  # (-1, 128)
        self.m = tf.placeholder(tf.float32, name='margin')

        self.build_magan()  # build MAGAN model

    def encoder(self, x, reuse=None):
        """
        (64)4c2s - (128)4c2s - (256)4c2s
        :param x: images
        :param reuse: re-usable
        :return: logits
        """
        with tf.variable_scope('encoder', reuse=reuse):
            x = t.conv2d(x, self.df_dim * 1, 4, 2, name='enc-conv2d-1')
            x = tf.nn.leaky_relu(x)

            x = t.conv2d(x, self.df_dim * 2, 4, 2, name='enc-conv2d-2')
            x = t.batch_norm(x, name='enc-bn-1')
            x = tf.nn.leaky_relu(x)

            x = t.conv2d(x, self.df_dim * 4, 4, 2, name='enc-conv2d-3')
            x = t.batch_norm(x, name='enc-bn-2')
            x = tf.nn.leaky_relu(x)

            return x

    def decoder(self, x, reuse=None):
        """
        (128)4c2s - (64)4c2s - (3)4c2s
        :param x: embeddings
        :param reuse: re-usable
        :return: prob
        """
        with tf.variable_scope('decoder', reuse=reuse):
            x = t.deconv2d(x, self.df_dim * 2, 4, 2, name='dec-deconv2d-1')
            x = t.batch_norm(x, name='dec-bn-1')
            x = tf.nn.leaky_relu(x)

            x = t.deconv2d(x, self.df_dim * 1, 4, 2, name='dec-deconv2d-2')
            x = t.batch_norm(x, name='dec-bn-2')
            x = tf.nn.leaky_relu(x)

            x = t.deconv2d(x, self.channel, 4, 2, name='dec-deconv2d-3')
            x = tf.nn.sigmoid(x)

            return x

    def discriminator(self, x, reuse=None):
        """
        # referred architecture in the paper
        : (64)4c2s-FC32-FC64*14*14_BR-(1)4dc2s_S
        :param x: images
        :param reuse: re-usable
        :return: prob, embeddings, gen-ed_image
        """
        with tf.variable_scope("discriminator", reuse=reuse):
            embeddings = self.encoder(x, reuse=reuse)
            decoded = self.decoder(embeddings, reuse=reuse)

            return embeddings, decoded

    def generator(self, z, reuse=None, is_train=True):
        """
        # referred architecture in the paper
        : (512)fc - (256)4c2s - (128)4c2s (3)4c2s
        :param z: embeddings
        :param reuse: re-usable
        :param is_train: trainable
        :return: prob
        """
        with tf.variable_scope("generator", reuse=reuse):
            assert self.fc_unit == 4 * 4 * self.gf_dim // 2

            x = t.dense(z, self.fc_unit, name='gen-fc-1')
            x = t.batch_norm(x, is_train=is_train, name='gen-bn-1')
            x = tf.nn.leaky_relu(x)

            x = tf.reshape(x, (-1, 4, 4, self.gf_dim // 2))

            x = t.deconv2d(x, self.gf_dim * 4, 4, 2, name='gen-deconv2d-1')
            x = t.batch_norm(x, is_train=is_train, name='gen-bn-2')
            x = tf.nn.leaky_relu(x)

            x = t.deconv2d(x, self.gf_dim * 2, 4, 2, name='gen-deconv2d-2')
            x = t.batch_norm(x, is_train=is_train, name='gen-bn-3')
            x = tf.nn.leaky_relu(x)

            x = t.deconv2d(x, self.channel, 4, 2, name='gen-deconv2d-3')
            x = tf.nn.tanh(x)

            return x

    def build_magan(self):
        # Generator
        self.g = self.generator(self.z)
        self.g_test = self.generator(self.z, reuse=True, is_train=False)

        # Discriminator
        _, d_real = self.discriminator(self.x)
        _, d_fake = self.discriminator(self.g, reuse=True)

        self.d_real_loss = t.mse_loss(self.x, d_real)
        self.d_fake_loss = t.mse_loss(self.g, d_fake)
        self.d_loss = self.d_real_loss + tf.maximum(0., self.m - self.d_fake_loss)
        self.g_loss = self.d_fake_loss

        # Summary
        tf.summary.scalar("loss/d_loss", self.d_loss)
        tf.summary.scalar("loss/d_real_loss", self.d_real_loss)
        tf.summary.scalar("loss/d_fake_loss", self.d_fake_loss)
        tf.summary.scalar("loss/g_loss", self.g_loss)

        # Optimizer
        t_vars = tf.trainable_variables()
        d_params = [v for v in t_vars if v.name.startswith('d')]
        g_params = [v for v in t_vars if v.name.startswith('g')]

        self.d_real_op = tf.train.AdamOptimizer(learning_rate=self.d_lr,
                                                beta1=self.beta1, beta2=self.beta2).minimize(self.d_real_loss,
                                                                                             var_list=d_params)

        self.d_op = tf.train.AdamOptimizer(learning_rate=self.d_lr,
                                           beta1=self.beta1, beta2=self.beta2).minimize(self.d_loss,
                                                                                        var_list=d_params)
        self.g_op = tf.train.AdamOptimizer(learning_rate=self.g_lr,
                                           beta1=self.beta1, beta2=self.beta2).minimize(self.g_loss,
                                                                                        var_list=g_params)

        # Merge summary
        self.merged = tf.summary.merge_all()

        # Model saver
        self.saver = tf.train.Saver(max_to_keep=1)
        self.writer = tf.summary.FileWriter('./model/', self.s.graph)
