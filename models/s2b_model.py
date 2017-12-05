import tensorflow as tf
from tensorpack import InputDesc, ModelDesc
from tensorpack.models.regularize import Dropout as tpDropout

import model_architectures as archs

from utils.tfutils import narrow_truncated_normal_initializer
from utils.bitmoji_api import BITMOJI_PARAM_SPLIT, BITMOJI_PARAM_SIZE
from utils import vae_gan


# noinspection PyMethodMayBeStatic
class Selfie2BitmojiModel(ModelDesc):
    """
    The "e" network from the paper. Trained to emulate the Bitmoji rendering
    engine by producing images from parameters.
    """
    def __init__(self, args):
        """
        :param args: The cli arguments.
        """
        self.args = args

    def _get_inputs(self):
        """
        :return: The input descriptions for TensorPack.
        """
        return [InputDesc(tf.float32, (None,) + archs.IMG_DIMS, 'Face_Images'),
                InputDesc(tf.float32, (None,) + archs.IMG_DIMS, 'Bitmoji_Images')]

    def _build_graph(self, inputs):
        """
        Construct the graph and define self.cost.

        :param inputs: The input tensors fed in by TensorPack. A batch of real
                       face images.
        """
        face_imgs, bitmoji_imgs = inputs

        # Pipeline from face image to Bitmoji
        face_encodings = self._face_encoder(face_imgs)
        gen_faces = self._generator(face_encodings)
        params = self._param_encoder(gen_faces)
        avatar_synth_faces = self._avatar_synth(params)

        # GAN discriminator predictions
        d_preds_real = self._discriminator(bitmoji_imgs)
        d_preds_fake = self._discriminator(gen_faces)

        # Other misc results for losses
        gen_face_encodings = self._face_encoder(gen_faces)
        regen_bitmoji = self._generator(self._face_encoder(bitmoji_imgs))

        batch_size, height, width, channels = gen_faces.shape
        gen_faces_left_shift = tf.concat([gen_faces[:, :,1:,:],
                                          tf.zeros((batch_size, height, 1, channels))],
                                         axis=2)
        gen_faces_up_shift = tf.concat([gen_faces[:, 1:,:,:],
                                        tf.zeros((batch_size, 1, width, channels))],
                                       axis=1)

        ##
        # Losses:
        ##

        # L2 diff between first generated image and image generated by running
        # parameters through e. Enforces that G learns to generate images close
        # to those generated by e
        self.l_c = tf.reduce_mean(tf.square(gen_faces - avatar_synth_faces),
                                  name='L_c')

        # L2 loss between the embedding of the input image and the embedding of
        # the first generated image. Generator learns to maintain structural
        # information from the embedding.
        self.l_const = tf.reduce_mean(tf.square(face_encodings - gen_face_encodings),
                                      name='L_const')

        # Regular ol' gan loss. Enforces that generator generates in the style
        # of the target images
        self.l_gan_d = tf.add(tf.reduce_mean(tf.log(1 - d_preds_real)),
                              tf.reduce_mean(tf.log(d_preds_real)),
                              name='L_gan_d')
        self.l_gan_g = tf.add(tf.reduce_mean(tf.log(1 - d_preds_fake)),
                              tf.reduce_mean(tf.log(d_preds_fake)),
                              name='L_gan_g')

        # L2 loss between rendered Bitmoji images and those same images
        # regenerated (fed through the face encoder and generator). Encourages
        # the generator to be the identity function for Bitmoji images.
        self.l_tid = tf.reduce_mean(tf.square(bitmoji_imgs - regen_bitmoji),
                                    name='L_tid')

        # Sum of the pixel-wise gradients
        self.l_tv = tf.reduce_mean(tf.sqrt(tf.square(gen_faces_left_shift - gen_faces) +
                                           tf.square(gen_faces_up_shift - gen_faces)),
                                   name='L_tv')

        with tf.name_scope('Summaries'):
            pred_comp = tf.concat([face_imgs, gen_faces, avatar_synth_faces], axis=2)
            tf.summary.image('Preds', pred_comp)

            tf.summary.scalar('L_c', self.l_c)
            tf.summary.scalar('L_const', self.l_const)
            tf.summary.scalar('L_gan_d', self.l_gan_d)
            tf.summary.scalar('L_gan_g', self.l_gan_g)
            tf.summary.scalar('L_tid', self.l_tid)
            tf.summary.scalar('L_tv', self.l_tv)

    def _get_optimizer(self):
        self.lr = tf.Variable(self.args.lr_g, trainable=False, name='Avatar_Synth/LR')
        print(self.lr.name)
        return tf.train.AdamOptimizer(learning_rate=self.lr)

    ##
    # Models
    ##

    def _face_encoder(self, imgs):
        """
        Constructs and computes the face encoder model. Architecture taken from
        https://github.com/zhangqianhui/vae-gan-tensorflow

        :param imgs: The batch of images to encode into facial features.

        :return: A batch of facial feature encodings for imgs.
        """
        with tf.variable_scope('Face_Encoder/Encode', reuse=tf.AUTO_REUSE):
            conv1 = tf.nn.relu(vae_gan.batch_normal(vae_gan.conv2d(
                imgs, output_dim=64, name='e_c1'), scope='e_bn1'))
            conv2 = tf.nn.relu(vae_gan.batch_normal(vae_gan.conv2d(
                conv1, output_dim=128, name='e_c2'), scope='e_bn2'))
            conv3 = tf.nn.relu(vae_gan.batch_normal(vae_gan.conv2d(
                conv2, output_dim=256, name='e_c3'), scope='e_bn3'))
            conv3 = tf.reshape(conv3, [-1, 256 * 8 * 8])

            fc1 = tf.nn.relu(vae_gan.batch_normal(vae_gan.fully_connect(
                conv3, output_size=1024, scope='e_f1'), scope='e_bn4'))

            z_mean = vae_gan.fully_connect(fc1, output_size=archs.FACE_ENCODING_SIZE, scope='e_f2')
            z_sigma = vae_gan.fully_connect(fc1, output_size=archs.FACE_ENCODING_SIZE, scope='e_f3')

            z_x = tf.add(z_mean, (tf.sqrt(tf.exp(z_sigma)) *
                                  tf.random_normal(shape=(self.args.batch_size, archs.FACE_ENCODING_SIZE))))

            return z_x

    def _generator(self, encodings):
        """
        Constructs and computes the generator model.

        :param encodings: A batch of facial feature encodings from which to to
                          generate Bitmoji-style faces.

        :return: A batch of Bitmoji-style faces generated from encodings.
        """
        with tf.variable_scope('Generator', reuse=tf.AUTO_REUSE):
            arch = archs.generator_model

            # Unshared fc layer to make sure face and bitmoji encodings (different sizes) can both
            # be used as inputs
            preds = tf.layers.dense(encodings,
                                    archs.GENERATOR_INPUT_SIZE,
                                    activation=tf.nn.relu,
                                    kernel_initializer=narrow_truncated_normal_initializer,
                                    bias_initializer=tf.zeros_initializer,
                                    name='FC',
                                    reuse=False)
            preds = tf.reshape(preds, (-1, 1, 1, archs.GENERATOR_INPUT_SIZE))

            for i in xrange(len(arch['conv_filters']) - 1):
                # Apply ReLU on all but the last layer
                activation = tf.nn.relu
                if i == len(arch['conv_filters']) - 2:
                    activation = tf.nn.tanh

                preds = tf.layers.conv2d_transpose(
                    preds,
                    arch['conv_filters'][i + 1],
                    arch['filter_widths'][i],
                    arch['strides'][i],
                    padding=arch['padding'][i],
                    activation=activation,
                    kernel_initializer=narrow_truncated_normal_initializer,
                    bias_initializer=tf.zeros_initializer,
                    name='Deconv_' + str(i),
                )

                preds = tf.layers.conv2d_transpose(
                    preds,
                    arch['conv_filters'][i + 1],
                    1,
                    1,
                    padding='SAME',
                    activation=tf.nn.relu,
                    kernel_initializer=narrow_truncated_normal_initializer,
                    bias_initializer=tf.zeros_initializer,
                    name='Conv_' + str(i),
                )

                # Apply batch norm on all but the last layer
                if i < len(arch['conv_filters']) - 2:
                    preds = tf.layers.batch_normalization(preds, name='BN_' + str(i))
                    preds = tpDropout(preds, keep_prob=self.args.keep_prob)

        return preds

    def _discriminator(self, imgs):
        """
        Constructs and computes the discriminator model.

        :param imgs: A batch of real or generated images.

        :return: A batch of predictions, whether each image in imgs is real or
                 generated.
        """
        with tf.variable_scope('Discriminator', reuse=tf.AUTO_REUSE):
            arch = archs.avatar_synth_model

            preds = imgs
            for i in xrange(len(arch['conv_filters']) - 1):
                # Apply leaky ReLU on all but the last layer
                activation = tf.nn.leaky_relu
                if i == len(arch['conv_filters']) - 2:
                    activation = tf.nn.sigmoid

                preds = tf.layers.conv2d(
                    preds,
                    arch['conv_filters'][i + 1],
                    arch['filter_widths'][i],
                    arch['strides'][i],
                    padding=arch['padding'][i],
                    activation=activation,
                    kernel_initializer=narrow_truncated_normal_initializer,
                    bias_initializer=tf.zeros_initializer,
                    name='Conv_' + str(i),
                )

                # Apply batch norm and dropout on all but the last layer
                if i < len(arch['conv_filters']) - 2:
                    preds = tf.layers.batch_normalization(preds, name='BN_' + str(i))
                    preds = tpDropout(preds, keep_prob=self.args.keep_prob)

        return preds

    # TODO: Pretrain this with supervised data?
    def _param_encoder(self, gen_faces):
        """
        Constructs and computes the parameter encoder model.

        :param gen_faces: A batch of Bitmoji-style faces generated by the
                          generator model.

        :return: A batch of predicted Bitmoji parameter vectors for gen_faces.
        """
        with tf.variable_scope('Param_Encoder', reuse=tf.AUTO_REUSE):
            arch = archs.param_encoder_model

            preds = gen_faces
            for i in xrange(len(arch['conv_filters']) - 1):
                # Apply leaky ReLU on all but the last layer
                activation = tf.nn.leaky_relu
                if i == len(arch['conv_filters']) - 2:
                    activation = tf.nn.sigmoid

                preds = tf.layers.conv2d(
                    preds,
                    arch['conv_filters'][i + 1],
                    arch['filter_widths'][i],
                    arch['strides'][i],
                    padding=arch['padding'][i],
                    activation=activation,
                    kernel_initializer=narrow_truncated_normal_initializer,
                    bias_initializer=tf.zeros_initializer,
                    name='Conv_' + str(i),
                )

                # Apply batch norm and dropout on all but the last layer
                if i < len(arch['conv_filters']) - 2:
                    preds = tf.layers.batch_normalization(preds, name='BN_' + str(i))
                    preds = tpDropout(preds, keep_prob=self.args.keep_prob)

            # Split param types and softmax to get binary vector with only one
            # truth value for each param type
            preds = tf.layers.flatten(preds, name='Flatten')
            param_sets = tf.split(preds, BITMOJI_PARAM_SPLIT, axis=1, name='Split')
            # TODO: softmax with low temp to act as sharp max?
            preds = tf.concat([tf.nn.softmax(param_set) for param_set in param_sets], 1,
                              name='Concat')


        return preds

    def _avatar_synth(self, params):
        """
        Constructs and computes the avatar synthesis model. This should be
        pretrained by run_avatar_synth.py and not trained in this complete
        model.

        :param params: The Bitmoji parameters to synthesize into Bitmoji images.

        :return: A batch of Bitmoji images synthesized from params.
        """
        with tf.variable_scope('Avatar_Synth', reuse=tf.AUTO_REUSE):
            arch = archs.avatar_synth_model

            # Reshape params into a 1x1 'image' for convolution
            preds = tf.reshape(params, (-1, 1, 1, BITMOJI_PARAM_SIZE))
            for i in xrange(len(arch['conv_filters']) - 1):
                # Apply ReLU on all but the last layer
                activation = tf.nn.relu
                if i == len(arch['conv_filters']) - 2:
                    activation = tf.nn.tanh

                preds = tf.layers.conv2d_transpose(
                    preds,
                    arch['conv_filters'][i + 1],
                    arch['filter_widths'][i],
                    arch['strides'][i],
                    padding=arch['padding'][i],
                    activation=tf.nn.relu,
                    name='Deconv_' + str(i),
                    trainable=False
                )
                self.preds = tf.layers.conv2d_transpose(
                    self.preds,
                    arch['conv_filters'][i + 1],
                    1,
                    1,
                    padding='SAME',
                    activation=activation,
                    kernel_initializer=narrow_truncated_normal_initializer,
                    bias_initializer=tf.zeros_initializer,
                    name='Conv_' + str(i),
                    trainable=False
                )

                # Apply batch norm on all but the last layer
                if i < len(arch['conv_filters']) - 2:
                    preds = tf.layers.batch_normalization(preds, name='BN_' + str(i))

        return preds
