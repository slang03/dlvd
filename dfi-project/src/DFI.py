from time import time

import tensorflow as tf
from __builtin__ import xrange
from sklearn.neighbors import KNeighborsClassifier
from tensorflow.contrib.opt import ScipyOptimizerInterface

from utils import *
from vgg19 import Vgg19


class DFI:
    """Deep Feature Interpolation procedure

    Implementation of DFI as described here:
    https://arxiv.org/pdf/1611.05507v1.pdf
    """

    def __init__(self, k=10, alpha=0.4, lamb=0.001, beta=2,
                 model_path="./model/vgg19.npy", num_layers=3,
                 gpu=True, data_dir='./data'):
        """
        Initialize the DFI procedure
        :param k: Number of nearest neighbours
        :param alpha: Scalar factor for w
        :param lamb: Scalar factor for total variation in loss
        :param beta: Scalar in exponent of total variation
        :param model_path: Path to the vgg19 pretrained model
        :param num_layers: Number of layers of which the deep
        features shall be extracted
        :param gpu: Compute or gpu
        :param data_dir: Directory for images
        """
        self._k = k
        self._alpha = alpha
        self._beta = beta
        self._lamb = lamb
        self._num_layers = num_layers
        self._model = load_model(model_path)
        self._gpu = gpu
        self._conv_layer_tensors = []
        self._data_dir = data_dir

        self._tensor_names = ['conv3_1/Relu:0', 'conv4_1/Relu:0',
                              'conv5_1/Relu:0']
        self._sess = None

        # Set device
        device = '/gpu:0' if self._gpu else '/cpu:0'

        # Setup
        with tf.device(device):
            self._graph = tf.Graph()
            with self._graph.as_default():
                self._nn = Vgg19(model=self._model)

        print('Initialization finished')

    def run(self, feat='No Beard', person_index=0):
        """

        :param feat: Attribute
        :param person_index: Index of start image
        :return:
        """
        print('Starting DFI')
        # Config for gpu
        config = tf.ConfigProto()
        if self._gpu:
            config.gpu_options.allow_growth = False
            config.gpu_options.per_process_gpu_memory_fraction = 0.80

        # Run the graph in the session.
        with tf.Session(graph=self._graph, config=config) as self._sess:
            self._sess.run(tf.global_variables_initializer())

            self._conv_layer_tensors = [
                self._graph.get_tensor_by_name(self._tensor_names[idx]) for idx
                in range(self._num_layers)]

            atts = load_discrete_lfw_attributes(self._data_dir)
            imgs_path = atts['path'].values
            start_img = reduce_img_size(load_images(*[imgs_path[0]]))[0]

            # Get image paths
            pos_paths, neg_paths = self._get_sets(atts, feat, person_index)

            # Reduce image sizes
            pos_imgs = reduce_img_size(load_images(*pos_paths))
            neg_imgs = reduce_img_size(load_images(*neg_paths))

            # Get pos/neg deep features
            pos_deep_features = self._phi(pos_imgs)
            neg_deep_features = self._phi(neg_imgs)

            # Calc W
            w = np.mean(pos_deep_features, axis=0) - np.mean(neg_deep_features,
                                                             axis=0)
            w /= np.linalg.norm(w)

            # Calc phi(z)
            phi_z = self._phi(start_img) + self._alpha * w

            initial_guess = np.array(start_img).reshape(-1)

            # Variable which is to be optimized
            tf_z = tf.Variable(start_img, 'z', dtype=tf.float32)

            # Define loss
            loss = self._minimize_z_tensor(phi_z, tf_z)

            # Run optimization steps in tensorflow
            optimizer = ScipyOptimizerInterface(loss, options={'maxiter': 10})
            self._sess.run(tf.global_variables_initializer())
            print('Starting minimization')
            optimizer.minimize(self._sess, feed_dict={
                self._nn.inputRGB: [start_img]
            })

            # Obtain Z
            z = self._sess.run(tf_z)

            # Dump result to 'z.npy'
            np.save('z', z)

    def _minimize_z_tensor(self, phi_z, tf_z):
        """
        Objective function implemented with tensors
        :param phi_z: phi(x) + alpha*w
        :param tf_z: Variable
        :return: loss
        """
        # Init z with the initial guess
        tf_phi_z = tf.constant(phi_z, dtype=tf.float32)
        phi_tf = self._phi_tensor()
        subtract = tf.subtract(phi_tf, tf_phi_z)
        square = tf.square(subtract)
        reduce_sum = tf.reduce_sum(square)
        loss_first = tf.scalar_mul(0.5, reduce_sum)
        regularization = self._total_variation_regularization(tf_z, self._beta)
        tv_loss = tf.scalar_mul(self._lamb, regularization)
        loss = tf.add(loss_first, tv_loss)
        return loss

    def _total_variation_regularization(self, x, beta=1):
        """
        Idea from:
        https://github.com/antlerros/tensorflow-fast-neuralstyle/blob/master/net.py
        """
        wh = tf.constant([[[[1], [1], [1]]], [[[-1], [-1], [-1]]]], tf.float32)
        ww = tf.constant([[[[1], [1], [1]], [[-1], [-1], [-1]]]], tf.float32)
        tvh = lambda x: self._conv2d(x, wh, p='SAME')
        tvw = lambda x: self._conv2d(x, ww, p='SAME')
        dh = tvh(x)
        dw = tvw(x)
        tv = (tf.add(tf.reduce_sum(dh ** 2, [1, 2, 3]),
                     tf.reduce_sum(dw ** 2, [1, 2, 3]))) ** (beta / 2.)
        return tv

    def _conv2d(self, x, W, strides=[1, 1, 1, 1], p='SAME', name=None):
        """2d Convolution"""
        new_shape = tf.TensorShape([tf.Dimension(1)] + x.get_shape().dims)
        x = tf.reshape(x, new_shape)
        return tf.nn.conv2d(x, W, strides=strides, padding=p, name=name)

    def _phi_tensor(self):
        """
        Implementation of the deep feature function in tensorflow
        :return: tensor of z in the deep feature space
        """
        # Start with first tensor
        res = tf.reshape(self._conv_layer_tensors[0], [-1])

        # Concatenate the rest
        for i in xrange(1, self._num_layers):
            tmp = tf.reshape(self._conv_layer_tensors[i], [-1])
            res = tf.concat(0, [res, tmp])
        return res

    def _phi(self, imgs):
        """Transform list of images into deep feature space

        :param imgs: input images
        :return: deep feature transformed images
        """

        if not isinstance(imgs, list):
            input_images = [imgs]
        else:
            input_images = imgs

        t0 = time()
        ret = self._sess.run(self._conv_layer_tensors,
                             feed_dict={
                                 self._nn.inputRGB: input_images
                             })
        t1 = time()
        print('Took {}'.format(t1 - t0))
        res = []

        # Create result list
        for img_idx in range(len(input_images)):
            phi_img = np.array([])

            # Append all layer results to a (M,) vector
            for layer_idx in range(self._num_layers):
                phi_img = np.append(phi_img,
                                    ret[layer_idx][img_idx].reshape(-1))

            res.append(phi_img)

        # Handle correct return type and normalize (L2)
        if not isinstance(imgs, list):
            return np.linalg.norm(res[0])  # Single image
        else:
            return [np.linalg.norm(x) for x in res]  # List of images

    def _minimize_z(self, z, phi_z, lamb, beta):
        """
        Calculates the loss as described in the paper
        :param z: Objective variable
        :param phi_z: phiz
        :param lamb: lambda
        :param beta: beta
        :return: loss
        """
        # Reshape into image form
        z = z.reshape(224, 224, 3)

        loss = 0.5 * np.linalg.norm(phi_z - self._phi(z)) ** 2
        total_variation = lamb * self._R(z, beta)
        res = loss + total_variation
        print(loss)
        print(total_variation)
        # print(res)
        return res

    def _R(self, z, beta):
        """
        Total Variation regularizer
        :param z: objective
        :param beta: beta
        :return: R
        """
        result = 0
        for i in range(z.shape[0] - 1):
            for j in range(z.shape[1] - 1):
                var = np.linalg.norm(z[i][j + 1] - z[i][j]) ** 2 + \
                      np.linalg.norm(z[i + 1][j] - z[i][j]) ** 2

                result += var ** (beta * 0.5)

        # normalize R
        result /= np.prod(z.shape, dtype=np.float32)

        return result

    def _get_sets(self, atts, feat, person_index):
        """
        Generates the subsets of the knn given a person index and a feature
        :param atts: Attribute dataframe
        :param feat: Feature
        :param person_index: Person index
        :return: Positive and negative subset of the k nearest neighbours of the given person
        """
        person = atts.loc[person_index]
        del person['person']
        del person['path']

        # Remove person from df
        atts = atts.drop(person_index)

        # Split by feature
        pos_set = atts.loc[atts[feat] == 1]
        neg_set = atts.loc[atts[feat] == -1]
        pos_paths = self._get_k_neighbors(pos_set, person)
        neg_paths = self._get_k_neighbors(neg_set, person)

        return pos_paths.as_matrix(), neg_paths.as_matrix()

    def _get_k_neighbors(self, subset, person):
        """
        Gets the KNN, given the subset and the person
        :param subset: Subset in which the knn should look at
        :param person: Starting point
        :return: Image paths of the k nearest neighbours of the person in the subset
        """
        del subset['person']
        paths = subset['path']
        del subset['path']

        knn = KNeighborsClassifier(n_jobs=4)
        dummy_target = [0 for x in range(subset.shape[0])]
        knn.fit(X=subset.as_matrix(), y=dummy_target)
        knn_indices = knn.kneighbors(person.as_matrix(), n_neighbors=self._k,
                                     return_distance=False)[0]

        neighbor_paths = paths.iloc[knn_indices]

        return neighbor_paths

    def attributes(self):
        """
        Generate a list of possible attributes to choose from
        :return: List of attributes
        """
        print('TODO: Implement')
        pass
