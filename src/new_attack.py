## dropout_detect.py -- break a dropout randomization detector
##
## Copyright (C) 2017, Nicholas Carlini <nicholas@carlini.com>.
##
## This program is licenced under the BSD 2-Clause licence,
## contained in the LICENCE file in this directory.

from __future__ import print_function
import numpy as np

from keras.models import Sequential
from keras.layers import Dense, Dropout, Activation, Flatten
from keras.layers import Conv2D, MaxPooling2D
from keras.utils import np_utils, plot_model
from keras.layers.core import Lambda
from keras.callbacks import LearningRateScheduler
from keras.optimizers import SGD
import keras
from utils import *

import tensorflow as tf
from setup_mnist import MNIST, MNISTModel
from setup_cifar import CIFAR, CIFARModel
import os

import sys
sys.path.append("../..")
from l2_attack import CarliniL2

from glue import BNN
import matplotlib
import matplotlib.pyplot as plt
from sklearn.metrics import auc, accuracy_score

def show(img):
    remap = "  .*#"+"#"*100
    img = (img.flatten()+.5)*3
    print("START")
    for i in range(28):
        print("".join([remap[int(round(x))] for x in img[i*28:i*28+28]]))

BINARY_SEARCH_STEPS = 9  # number of times to adjust the constant with binary search
MAX_ITERATIONS = 10000   # number of iterations to perform gradient descent
ABORT_EARLY = True       # if we stop improving, abort gradient descent early
LEARNING_RATE = 1e-2     # larger values converge faster to less accurate results
TARGETED = True          # should we target one specific class? or just be wrong?
CONFIDENCE = 0           # how strong the adversarial example should be
INITIAL_CONST = 1e-3     # the initial constant c to pick as a first guess
ISMNIST = False
class CarliniL2Multiple:
    def __init__(self, sess, models, batch_size=1, confidence = CONFIDENCE,
                 targeted = TARGETED, learning_rate = LEARNING_RATE,
                 binary_search_steps = BINARY_SEARCH_STEPS, max_iterations = MAX_ITERATIONS,
                 abort_early = ABORT_EARLY, 
                 initial_const = INITIAL_CONST):
        """
        The L_2 optimized attack. 

        This attack is the most efficient and should be used as the primary 
        attack to evaluate potential defenses.

        Returns adversarial examples for the supplied model.

        confidence: Confidence of adversarial examples: higher produces examples
          that are farther away, but more strongly classified as adversarial.
        batch_size: Number of attacks to run simultaneously.
        targeted: True if we should perform a targetted attack, False otherwise.
        learning_rate: The learning rate for the attack algorithm. Smaller values
          produce better results but are slower to converge.
        binary_search_steps: The number of times we perform binary search to
          find the optimal tradeoff-constant between distance and confidence. 
        max_iterations: The maximum number of iterations. Larger values are more
          accurate; setting too small will require a large learning rate and will
          produce poor results.
        abort_early: If true, allows early aborts if gradient descent gets stuck.
        initial_const: The initial tradeoff-constant to use to tune the relative
          importance of distance and confidence. If binary_search_steps is large,
          the initial constant is not important.
        """

        image_size, num_channels, num_labels = models[0].image_size, models[0].num_channels, models[0].num_labels
        self.sess = sess
        self.TARGETED = targeted
        self.LEARNING_RATE = learning_rate
        self.MAX_ITERATIONS = max_iterations
        self.BINARY_SEARCH_STEPS = binary_search_steps
        self.ABORT_EARLY = abort_early
        self.CONFIDENCE = confidence
        self.initial_const = initial_const
        self.batch_size = batch_size

        self.repeat = binary_search_steps >= 10

        shape = (batch_size,image_size,image_size,num_channels)
        
        # the variable we're going to optimize over
        modifier = tf.Variable(np.zeros(shape,dtype=np.float32))

        # these are variables to be more efficient in sending data to tf
        self.timg = tf.Variable(np.zeros(shape), dtype=tf.float32)
        self.tlab = tf.Variable(np.zeros((batch_size,num_labels)), dtype=tf.float32)
        self.const = tf.Variable(np.zeros(batch_size), dtype=tf.float32)

        # and here's what we use to assign them
        self.assign_timg = tf.placeholder(tf.float32, shape)
        self.assign_tlab = tf.placeholder(tf.float32, (batch_size,num_labels))
        self.assign_const = tf.placeholder(tf.float32, [batch_size])
        
        # the resulting image, tanh'd to keep bounded from -0.5 to 0.5
        self.newimg = tf.tanh(modifier + self.timg)/2
        
        # prediction BEFORE-SOFTMAX of the model
        outs = []
        for model in models:
            outs.append(model.predict(self.newimg))
        self.outputs = tf.transpose(tf.stack(outs), [1, 0, 2])
        print(self.outputs.get_shape())
        
        # distance to the input data
        self.l2dist = tf.reduce_sum(tf.square(self.newimg-tf.tanh(self.timg)/2),[1,2,3])
        
        # compute the probability of the label class versus the maximum other
        real = tf.reduce_sum((self.tlab[:,tf.newaxis,:])*self.outputs,2)
        other = tf.reduce_max((1-self.tlab[:,tf.newaxis,:])*self.outputs - (self.tlab[:,tf.newaxis,:]*10000),2)

        print('real',real.get_shape())
        print('other',real.get_shape())

        if self.TARGETED:
            # if targetted, optimize for making the other class most likely
            loss1 = tf.maximum(0.0, other-real+self.CONFIDENCE)
        else:
            # if untargeted, optimize for making this class least likely.
            loss1 = tf.maximum(0.0, real-other+self.CONFIDENCE)

        print('l1',loss1.get_shape())

        # sum up the losses
        self.loss2 = tf.reduce_sum(self.l2dist)
        self.loss1 = tf.reduce_sum(self.const[:,tf.newaxis]*loss1)
        self.loss = self.loss1+self.loss2
        
        # Setup the adam optimizer and keep track of variables we're creating
        start_vars = set(x.name for x in tf.global_variables())
        optimizer = tf.train.AdamOptimizer(self.LEARNING_RATE)
        self.train = optimizer.minimize(self.loss, var_list=[modifier])
        end_vars = tf.global_variables()
        new_vars = [x for x in end_vars if x.name not in start_vars]

        # these are the variables to initialize when we run
        self.setup = []
        self.setup.append(self.timg.assign(self.assign_timg))
        self.setup.append(self.tlab.assign(self.assign_tlab))
        self.setup.append(self.const.assign(self.assign_const))
        
        self.init = tf.variables_initializer(var_list=[modifier]+new_vars)

    def attack(self, imgs, targets):
        """
        Perform the L_2 attack on the given images for the given targets.

        If self.targeted is true, then the targets represents the target labels.
        If self.targeted is false, then targets are the original class labels.
        """
        r = []
        print('go up to',len(imgs))
        for i in range(0,len(imgs),self.batch_size):
            print('tick',i)
            r.extend(self.attack_batch(imgs[i:i+self.batch_size], targets[i:i+self.batch_size]))
        return np.array(r)

    def attack_batch(self, imgs, labs):
        """
        Run the attack on a batch of images and labels.
        """
        def compare(x,y):
            if not isinstance(x, (float, int, np.int64)):
                x = np.copy(x)
                x[y] -= self.CONFIDENCE
                x = np.argmax(x)
            if self.TARGETED:
                return x == y
            else:
                return x != y

        batch_size = self.batch_size

        # convert to tanh-space
        imgs = np.arctanh(imgs*1.999999)

        # set the lower and upper bounds accordingly
        lower_bound = np.zeros(batch_size)
        CONST = np.ones(batch_size)*self.initial_const
        upper_bound = np.ones(batch_size)*1e10

        # the best l2, score, and image attack
        o_bestl2 = [1e10]*batch_size
        o_bestscore = [-1]*batch_size
        o_bestattack = [np.zeros(imgs[0].shape)]*batch_size
        
        for outer_step in range(self.BINARY_SEARCH_STEPS):
            #print(o_bestl2)
            # completely reset adam's internal state.
            self.sess.run(self.init)
            batch = imgs[:batch_size]
            batchlab = labs[:batch_size]
    
            bestl2 = [1e10]*batch_size
            bestscore = [-1]*batch_size

            # The last iteration (if we run many steps) repeat the search once.
            if self.repeat == True and outer_step == self.BINARY_SEARCH_STEPS-1:
                CONST = upper_bound
            print(CONST)

            # set the variables so that we don't have to send them over again
            self.sess.run(self.setup, {self.assign_timg: batch,
                                       self.assign_tlab: batchlab,
                                       self.assign_const: CONST})
            
            prev = 1e20
            for iteration in range(self.MAX_ITERATIONS):
                # perform the attack 
                _, l, l2s, scores, nimg = self.sess.run([self.train, self.loss, 
                                                         self.l2dist, self.outputs, 
                                                         self.newimg])

                #print(np.argmax(scores))
                # print out the losses every 10%
                if iteration%(self.MAX_ITERATIONS//10) == 0:
                    print(iteration,self.sess.run((self.loss,self.loss1,self.loss2)))

                # check if we should abort search if we're getting nowhere.
                if self.ABORT_EARLY and iteration%(self.MAX_ITERATIONS//10) == 0:
                    if l > prev*.9999:
                        break
                    prev = l

                # adjust the best result found so far
                for e,(l2,sc,ii) in enumerate(zip(l2s,scores,nimg)):
                    if l2 < bestl2[e] and np.mean([compare(x, np.argmax(batchlab[e])) for x in sc])>=.7:
                        bestl2[e] = l2
                        bestscore[e] = np.argmax(sc)
                    if l2 < o_bestl2[e] and np.mean([compare(x, np.argmax(batchlab[e])) for x in sc])>=.7:
                        o_bestl2[e] = l2
                        o_bestscore[e] = np.argmax(sc)
                        o_bestattack[e] = ii

            print('bestl2',bestl2)
            print('bestscore',bestscore)
            # adjust the constant as needed
            for e in range(batch_size):
                if bestscore[e] != -1:
                    # success, divide const by two
                    upper_bound[e] = min(upper_bound[e],CONST[e])
                    if upper_bound[e] < 1e9:
                        CONST[e] = (lower_bound[e] + upper_bound[e])/2
                else:
                    # failure, either multiply by 10 if no solution found yet
                    #          or do binary search with the known upper bound
                    lower_bound[e] = max(lower_bound[e],CONST[e])
                    if upper_bound[e] < 1e9:
                        CONST[e] = (lower_bound[e] + upper_bound[e])/2
                    else:
                        CONST[e] *= 10

        # return the best solution found
        o_bestl2 = np.array(o_bestl2)
        return o_bestattack

class Wrap:
    def __init__(self, model):
        self.image_size = 28 if ISMNIST else 32
        self.num_channels = 1 if ISMNIST else 3
        self.num_labels = 10
        self.model = model

    def predict(self, xs):
        return self.model(xs)

def make_model(Model, dropout=True, fixed=False):
    def Dropout(p):
        if not dropout: 
            p = 0
        def my_dropout(x):
            if fixed:
                shape = x.get_shape().as_list()[1:]
                keep = np.random.random(shape)>p
                return x*keep
            else:
                return tf.nn.dropout(x, 1-p)
        return keras.layers.core.Lambda(my_dropout)

    return Model(None, Dropout=Dropout).model
    

def compute_u(sess, modeld, data):
    T = 100
    ys = np.array(list(zip(*[sess.run(tf.nn.softmax(modeld.predict(data))) for _ in range(T)])))
    print(ys.shape)
    
    term1 = np.mean(np.sum(ys**2,axis=2),axis=1)

    term2 = np.sum(np.mean(ys,axis=1)**2,axis=1)
    print("uncertainties")
    print(term1 - term2)

    print('absolute mean uncertainty',np.mean(term1-term2))
    
    return term1-term2
    

def differentable_u(modeld, data, count):

    data = tf.tile(data, [count, 1, 1, 1])
    
    ys = tf.nn.softmax(modeld(data))

    ys = tf.reshape(ys, [count, -1, 10])
    ys = tf.transpose(ys, perm=[1, 0, 2])

    term1 = tf.reduce_mean(tf.reduce_sum(ys**2,axis=2),axis=1)

    term2 = tf.reduce_sum(tf.reduce_mean(ys,axis=1)**2,axis=1)
    
    return term1-term2

def differentiable_u_multiple(models, data):

    ys = []
    for model in models:
        ys.append(tf.nn.softmax(model(data)))

    ys = tf.stack(ys)
    ys = tf.transpose(ys, perm=[1, 0, 2])

    term1 = tf.reduce_mean(tf.reduce_sum(ys**2,axis=2),axis=1)

    term2 = tf.reduce_sum(tf.reduce_mean(ys,axis=1)**2,axis=1)
    
    return term1-term2


def create_filename(dataset, inf, threat, content):
    return "results/" + dataset + '_' + inf + '_' + threat + '_' + content
    
    
def gray_box(clean_x, clean_y, confidence, model):
    #hyperparams = init_hp...
    # model = Model.create_model()
    single_model = model.model_list[np.random.choice(len(model.model_list),1)[0]]#np.random.choice(model.model_list, 1)
    # print(type(single_model))
    # print(type(model.model))
    sess = keras.backend.get_session()
    attack = CarliniL2(sess, Wrap(single_model), batch_size=20, max_iterations=10000,#1000
                       binary_search_steps=9, learning_rate=1e-2, initial_const=1e-3,
                       targeted=False, confidence=confidence, abort_early=True)
    adv = attack.attack(clean_x, clean_y)
    return adv

def white_box(clean_x, clean_y, confidence, model):
    all_models = model.model_list
    indices = np.random.choice(len(model.model_list), size=50, replace=False)
    models = []
    for i in indices:
        models.append(all_models[i])
    # models = np.random.choice(all_models, size=20, replace=False)
    sess = keras.backend.get_session()
    attack = CarliniL2Multiple(sess, [Wrap(m) for m in models], batch_size=20, binary_search_steps=9,
                           initial_const=1e-3, max_iterations=10000, confidence=confidence, #1000 iters
                           targeted=False, abort_early=True, learning_rate=1e-2)
    adv = attack.attack(clean_x, clean_y)
    return adv

def eval_model(model, clean_x, clean_y, adv):
    #Accuracy measurements
    sess = keras.backend.get_session()
    adv_preds = model.model.predict(adv)
    adv_acc = np.mean(np.argmax(adv_preds,axis=1) == np.argmax(clean_y,axis=1))
    dist = np.mean([np.linalg.norm(clean_x[i]-adv[i]) for i in range(len(clean_x))])
    models = model.model_list
    if ISMNIST:
        p = tf.placeholder(tf.float32, (None, 28, 28, 1))
    else:
        p = tf.placeholder(tf.float32, (None, 32, 32, 3))
    r1 = differentiable_u_multiple(models, p)
    r2 = differentiable_u_multiple(models, p)
    clean_unc = sess.run(r1, {p: clean_x})
    adv_unc = sess.run(r2, {p: adv})
    #print('uncertainty on test data', np.mean((sess.run(r, {p: data.test_data[:N]}))))
    # clean_unc = differentiable_u_multiple(models, clean_x)
    # adv_unc = differentiable_u_multiple(models, adv)
    # roc_auc(clean_unc, adv_unc)
    # print("Adv. Acc, Distortion, Mean Clean Unc, Mean Adv Unc: ", adv_acc, dist, 
    #       np.mean(clean_unc), np.mean(adv_unc))
    return adv_acc, dist, (clean_unc, adv_unc)

def run_attacks():
    datasets = ["CIFAR10", "MNIST"]
    inf_methods = ["ADVI", "NUTS"]#, "HMC", "MCDROP"]
    colors = ["gray", "white"]
    confs = [0,0.25,0.5,1,2,4,8,12,16,24,32,48,64]
    for dataset in datasets:
        for inf in inf_methods:
            global ISMNIST
            if dataset == "MNIST":
                ISMNIST = True
                data = MNIST()
            else:
                ISMNIST = False
                data = CIFAR()
            #standardize naming of pkls between comps in some way
            path = "pkls/" + dataset + "-" + inf + ".zip"
            try:
                # print(path)
                model = BNN(path, ISMNIST=ISMNIST)
            except(FileNotFoundError):
                pass
            clean_x = data.test_data[:20]
            clean_y = data.test_labels[:20]
            g_results = [[],[],[]]
            w_results = [[],[],[]]
            for conf in confs:
                adv_gray_box = gray_box(clean_x, clean_y, conf, model)
                adv_white_box = white_box(clean_x, clean_y, conf, model)
                g_adv_acc, g_dist, g_uncs = eval_model(model,clean_x,clean_y, adv_gray_box)
                w_adv_acc, w_dist, w_uncs = eval_model(model,clean_x,clean_y, adv_white_box)
                g_results[0].append(g_adv_acc)
                g_results[1].append(g_dist)
                g_results[2].append(g_uncs)
                w_results[0].append(w_adv_acc)
                w_results[1].append(w_dist)
                w_results[2].append(w_uncs)
                #visualize adv examples
                adv_ex_g = adv_gray_box[0]
                adv_ex_w = adv_white_box[0]
                if dataset == "MNIST":
                    plt.gray()
                    adv_ex_g = adv_ex_g.reshape([28,28])
                    adv_ex_w = adv_ex_w.reshape([28,28])
                else:
                    #cifar10 images subtracted .5 in setup_cifar
                    #reshape(32,32,3)?
                    adv_ex_g += 0.5
                    adv_ex_w += 0.5
                plt.imshow(adv_ex_g)
                # plt.show()
                plt.savefig("adv_vis/" + dataset + "_" + inf + "_" + (str)(conf) + "_gray_adv_visualization.png")
                plt.imshow(adv_ex_w)
                plt.savefig("adv_vis/" + dataset + "_" + inf + "_" + (str)(conf) + "_white_adv_visualization.png")
                np.save(create_filename(dataset, inf, "gray", "adv_accs"), np.array(g_results[0]))
                np.save(create_filename(dataset, inf, "gray", "dists"), np.array(g_results[1]))
                np.save(create_filename(dataset, inf, "gray", "uncs"), np.array(g_results[2]))
                np.save(create_filename(dataset, inf, "white", "adv_accs"), np.array(w_results[0]))
                np.save(create_filename(dataset, inf, "white", "dists"), np.array(w_results[1]))
                np.save(create_filename(dataset, inf, "white", "uncs"), np.array(w_results[2]))
                # np.save(create_filename(dataset, inf, "white", "results"), np.array(w_results))
            for i in range(len(confs)):
                print("datatset: {}, inf_method: {}, threat: gray, conf: {}".format(dataset, inf, confs[i]))
                print("Adv. Acc: {}, Distortion: {}, Mean Clean Unc: {}, Mean Adv Unc: {}".format(g_results[0][i], g_results[1][i], 
                        np.mean(g_results[2][i][0]), np.mean(g_results[2][i][1])))
                print("datatset: {}, inf_method: {}, threat: white, conf: {}".format(dataset, inf, confs[i]))
                print("Adv. Acc: {}, Distortion: {}, Mean Clean Unc: {}, Mean Adv Unc: {}".format(w_results[0][i], w_results[1][i], 
                        np.mean(w_results[2][i][0]), np.mean(w_results[2][i][1])))

# confs = [0,1,2,3,4,5,6,7,8,9,10,20,50] 
def run_mc_drop():
    global ISMNIST
    ISMNIST = False
    keras.backend.set_learning_phase(False)
    model = make_model(CIFARModel, dropout=True)
    model.load_weights("models/MCDrop-cifar")
    data = CIFAR()
    confs = [0,0.25,0.5,1,2,4,8,12,16,24,32,48,64]
    clean_x = data.test_data[:20]
    clean_y = data.test_labels[:20]
    g_results = [[],[],[]]
    w_results = [[],[],[]]
    for conf in confs:
        adv_gray_box = mc_drop_gray_box(clean_x, clean_y, conf, CIFARModel)
        adv_white_box = mc_drop_white_box(clean_x, clean_y, conf, CIFARModel)
        g_adv_acc, g_dist, g_uncs = mc_drop_eval_model(CIFARModel,clean_x,clean_y, adv_gray_box)
        w_adv_acc, w_dist, w_uncs = mc_drop_eval_model(CIFARModel,clean_x,clean_y, adv_white_box)
        g_results[0].append(g_adv_acc)
        g_results[1].append(g_dist)
        g_results[2].append(g_uncs)
        w_results[0].append(w_adv_acc)
        w_results[1].append(w_dist)
        w_results[2].append(w_uncs)
        #visualize adv examples
        adv_ex_g = adv_gray_box[0]
        adv_ex_w = adv_white_box[0]
        dataset = "CIFAR"
        inf = "MCDROP"
        if dataset == "MNIST":
            plt.gray()
            adv_ex_g = adv_ex_g.reshape([28,28])
            adv_ex_w = adv_ex_w.reshape([28,28])
        else:
            #cifar10 images subtracted .5 in setup_cifar
            #reshape(32,32,3)?
            # adv_ex_g = adv_ex_g.reshape([32,32])
            # adv_ex_w = adv_ex_w.reshape([32,32])
            adv_ex_g += 0.5
            adv_ex_w += 0.5
        plt.imshow(adv_ex_g)
        # plt.show()
        plt.savefig("adv_vis/" + dataset + "_" + inf + "_" + (str)(conf) + "_gray_adv_visualization.png")
        plt.imshow(adv_ex_w)
        plt.savefig("adv_vis/" + dataset + "_" + inf + "_" + (str)(conf) + "_white_adv_visualization.png")
        np.save(create_filename(dataset, inf, "gray", "adv_accs"), np.array(g_results[0]))
        np.save(create_filename(dataset, inf, "gray", "dists"), np.array(g_results[1]))
        np.save(create_filename(dataset, inf, "gray", "uncs"), np.array(g_results[2]))
        np.save(create_filename(dataset, inf, "white", "adv_accs"), np.array(w_results[0]))
        np.save(create_filename(dataset, inf, "white", "dists"), np.array(w_results[1]))
        np.save(create_filename(dataset, inf, "white", "uncs"), np.array(w_results[2]))
        # np.save(create_filename(dataset, inf, "white", "results"), np.array(w_results))
    for i in range(len(confs)):
        print("datatset: {}, inf_method: {}, threat: gray, conf: {}".format(dataset, inf, confs[i]))
        print("Adv. Acc: {}, Distortion: {}, Mean Clean Unc: {}, Mean Adv Unc: {}".format(g_results[0][i], g_results[1][i], 
                np.mean(g_results[2][i][0]), np.mean(g_results[2][i][1])))
        print("datatset: {}, inf_method: {}, threat: white, conf: {}".format(dataset, inf, confs[i]))
        print("Adv. Acc: {}, Distortion: {}, Mean Clean Unc: {}, Mean Adv Unc: {}".format(w_results[0][i], w_results[1][i], 
                np.mean(w_results[2][i][0]), np.mean(w_results[2][i][1])))

def roc_auc(clean_us, adv_us):
    uncertainties = np.concatenate((np.stack((clean_us, np.zeros(len(clean_us))),axis=-1), np.stack((adv_us, np.ones(len(adv_us))),axis=-1)))
    # print(uncertainties)
    uncertainties = uncertainties[uncertainties[:,0].argsort()][::-1]
    # print(uncertainties)
    min_u = uncertainties[len(uncertainties)-1][0]
    max_u = uncertainties[0][0]
    u_range = max_u - min_u
    us = np.linspace(max_u, min_u, 50)
    TPRs = []
    FPRs = []
    i = 0
    FP = 0
    TP = 0
    num_clean = (float)(len(clean_us))
    num_adv = (float)(len(adv_us))
    for u in us:
        while uncertainties[i][0] > u:
            if uncertainties[i][1] == 0:
                FP += 1
            else:
                TP += 1
            i += 1
        TPRs.append(TP/num_adv)
        FPRs.append(FP/num_clean)
    # print("TPRS: \n{}".format(TPRs))
    # print("FPRS: \n{}".format(FPRs))
    auc_val = auc(FPRs + [1.0], TPRs + [1.0])
    return auc_val,  TPRs, FPRs
    # print("auc_val: {}".format(auc_val))


def mc_drop_gray_box(clean_x, clean_y, confidence, model):
    #hyperparams = init_hp...
    # model = Model.create_model()
    single_model = make_model(model, dropout=True)
    single_model.load_weights("models/MCDrop-cifar")    # print(type(single_model))
    # print(type(model.model))
    sess = keras.backend.get_session()
    attack = CarliniL2(sess, Wrap(single_model), batch_size=20, max_iterations=1000,#1000
                       binary_search_steps=3, learning_rate=1e-1, initial_const=1,
                       targeted=False, confidence=confidence, abort_early=True)
    adv = attack.attack(clean_x, clean_y)
    return adv

def mc_drop_white_box(clean_x, clean_y, confidence, model):
    models = []
    for _ in range(20):
        m = make_model(model, dropout=True, fixed=True)
        m.load_weights("models/MCDrop-cifar")
        models.append(m)
    # models = np.random.choice(all_models, size=20, replace=False)
    sess = keras.backend.get_session()
    attack = CarliniL2Multiple(sess, [Wrap(m) for m in models], batch_size=20, binary_search_steps=4,
                           initial_const=1, max_iterations=1000, confidence=confidence, #1000 iters
                           targeted=False, abort_early=True, learning_rate=1e-1)
    adv = attack.attack(clean_x, clean_y)
    return adv

def mc_drop_eval_model(model, clean_x, clean_y, adv):
    #Accuracy measurements
    models = []
    for _ in range(20):
        m = make_model(model, dropout=True, fixed=True)
        m.load_weights("models/MCDrop-cifar")
        models.append(m)
    #bad change to avg
    model = models[0]
    sess = keras.backend.get_session()
    adv_preds = model.predict(adv)
    adv_acc = np.mean(np.argmax(adv_preds,axis=1) == np.argmax(clean_y,axis=1))
    dist = np.mean([np.linalg.norm(clean_x[i]-adv[i]) for i in range(len(clean_x))])
    if ISMNIST:
        p = tf.placeholder(tf.float32, (None, 28, 28, 1))
    else:
        p = tf.placeholder(tf.float32, (None, 32, 32, 3))
    r1 = differentiable_u_multiple(models, p)
    r2 = differentiable_u_multiple(models, p)
    clean_unc = sess.run(r1, {p: clean_x})
    adv_unc = sess.run(r2, {p: adv})
    #print('uncertainty on test data', np.mean((sess.run(r, {p: data.test_data[:N]}))))
    # clean_unc = differentiable_u_multiple(models, clean_x)
    # adv_unc = differentiable_u_multiple(models, adv)
    # roc_auc(clean_unc, adv_unc)
    # print("Adv. Acc, Distortion, Mean Clean Unc, Mean Adv Unc: ", adv_acc, dist, 
    #       np.mean(clean_unc), np.mean(adv_unc))
    return adv_acc, dist, (clean_unc, adv_unc)

def plot_results(dataset, inf, color):
    path = "results/{}_{}_{}".format(dataset, inf, color)
    title_desc = "{} {} {}box".format(dataset, inf, color)
    filename = "plots/{}_{}_{}".format(dataset, inf, color)
    aucs = []
    max_auc = 0
    plot_TPRs = []
    plot_FPRs = []
    uncs_path = path + "_uncs.npy"
    dists_uncs = np.load(uncs_path)
    for uncs in dists_uncs:
        auc_val, TPRs, FPRs = roc_auc(uncs[0], uncs[1])
        if auc_val > max_auc:
            max_auc = auc_val
            plot_TPRs = TPRs
            plot_FPRs = FPRs
        aucs.append(auc_val)
    plt.plot(plot_FPRs + [1.0], plot_TPRs + [1.0])
    plt.title("ROC Curve for {}".format(title_desc))
    plt.xlabel("FPR")
    plt.ylabel("TPR")
    plt.savefig("{}_ROC.png".format(filename))
    plt.show()

    dists_path = path + "_dists.npy"
    dists = np.load(dists_path)
    zipped = sorted(zip(dists, aucs))
    plot_1_xy = ([a for a,b in zipped], [b for a,b in zipped])
    plt.plot(plot_1_xy[0], plot_1_xy[1])
    plt.title("AUC vs Distortion for {}".format(title_desc))
    plt.xlabel("Distortion")
    plt.ylabel("AUC Value")
    plt.savefig("{}_AUC_vs_dist.png".format(filename))
    plt.show()

    adv_accs_path = path + "_adv_accs.npy"
    adv_accs = np.load(adv_accs_path)
    zipped = sorted(zip(dists, adv_accs))
    plot_1_xy = ([a for a,b in zipped], [b for a,b in zipped])
    plt.plot(plot_1_xy[0], plot_1_xy[1])
    plt.title("Adversarial Accuracy vs Distortion for {}".format(title_desc))
    plt.xlabel("Distortion")
    plt.ylabel("Adv Accuracy")
    plt.savefig("{}_adv_acc_vs_dist.png".format(filename))
    plt.show()

    #All adv accuracies on same graph for each model + inf vs dist



def test(Model, data, path):
    keras.backend.set_learning_phase(False)
    model = make_model(Model, dropout=False)
    model.load_weights(path)
    print("Model type is: ", type(model))
    print(model.summary())
    print(model.input_shape)

    modeld = make_model(Model, dropout=True)
    modeld.load_weights(path)

    print("Data type is: ", type(data.test_data))
    print(data.test_data.shape)
    guess = model.predict(data.test_data)
    print(guess[:10])
    print('Accuracy without dropout',np.mean(np.argmax(guess,axis=1) == np.argmax(data.test_labels,axis=1)))

    guess = modeld.predict(data.test_data)
    print('Accuracy with dropout', np.mean(np.argmax(guess,axis=1) == np.argmax(data.test_labels,axis=1)))
    
    sess = keras.backend.get_session()

    N = 50
    labs = get_labs(data.test_data[:N])
    print(labs)
    print('good?',np.sum(labs*data.test_labels[:N]))

    attack = CarliniL2(sess, Wrap(model), batch_size=N, max_iterations=1000,
                       binary_search_steps=3, learning_rate=1e-1, initial_const=1,
                       targeted=True, confidence=0)
    adv = attack.attack(data.test_data[:N], labs)
    guess = model.predict(adv)
    mc_drop_preds = modeld.predict(adv)
    print('Accuracy of MC-dropout on gray-box attack',np.mean(np.argmax(mc_drop_preds,axis=1) == np.argmax(data.test_labels,axis=1)))
    print('average distortion',np.mean(np.sum((data.test_data[:N]-adv)**2,axis=(1,2,3))**.5))
    print(guess[:10])

    print("Test data")
    valid_u = compute_u(sess, modeld, data.test_data[:N])
    print("Adversarial examples")
    adv_u = compute_u(sess, modeld, adv)

    # The below attack may not even be necessary for CIFAR
    # the adversarial examples generated with (3,1000,1e-1) have a lower mean
    # uncertainty than the test images, but again with a 3x increase in distortion.

    if ISMNIST:
        p = tf.placeholder(tf.float32, (None, 28, 28, 1))
    else:
        p = tf.placeholder(tf.float32, (None, 32, 32, 3))
    r = differentable_u(modeld, p, 100)    

    models = []
    for _ in range(20):
        m = make_model(Model, dropout=True, fixed=True)
        m.load_weights(path)
        models.append(m)
    #r2 = differentable_u_multiple(models, p)

    #print('uncertainty on test data', np.mean((sess.run(r, {p: data.test_data[:N]}))))
    #print('uncertainty on test data (multiple models)', np.mean((sess.run(r2, {p: data.test_data[:N]}))))
    #print('labels on robust model', np.argmax(sess.run(robustmodel.predict(p), {p: data.test_data[:100]}),axis=1))
    
    attack = CarliniL2Multiple(sess, [Wrap(m) for m in models], batch_size=10, binary_search_steps=4,
                               initial_const=1, max_iterations=1000, confidence=1,
                               targeted=True, abort_early=False, learning_rate=1e-1)


    z = np.zeros((N, 10))
    z[np.arange(N),np.random.random_integers(0,9,N)]

    #qq = (3, 2, 1, 18, 4, 8, 11, 0, 61, 7)
    #np.save("images/mnist_dropout", attack.attack( = 1
    # z[np.arange(N),(9, 3, 0, 8, 7, 3, 4, 1, 6, 4)] = 1
    # print(z)data.test_data[qq,:,:,:],
    #                                               np.pad(np.roll(data.test_labels[qq,:],1,axis=1), [(0, 0), (0, 0)], 'constant')))
    #exit(0)

    
    adv = attack.attack(data.test_data[:N], labs)
    # adv = attack.attack(data.test_data[:N], data.test_labels[:N])

    np.save("dropout_adv_"+str(ISMNIST),adv)
    #adv = np.load("/tmp/qq.npy")
    
    guess = modeld.predict(adv)

    print('normal predictions',guess)

    print('average distortion',np.mean(np.sum((data.test_data[:N]-adv)**2,axis=(1,2,3))**.5))

    print('normal label predictions',np.argmax(guess,axis=1))

    for m in models:
        print('model preds',np.argmax(m.predict(adv),axis=1))
    
    print('Model accuracy on adversarial examples',np.mean(np.argmax(guess,axis=1)==np.argmax(data.test_labels[:N],axis=1)))

    adv_u = compute_u(sess, modeld, adv)
    #print('differentable uncertienty',np.mean((sess.run(r, {p: adv}))))

    print('Targeted adversarial examples success rate',np.mean(np.argmax(guess,axis=1)==np.argmax(z,axis=1)))

    import matplotlib
    import matplotlib.pyplot as plt
    from matplotlib.backends.backend_pdf import PdfPages

    """
    fig = plt.figure(figsize=(4,3))
    fig.subplots_adjust(bottom=0.15,left=.15)
    a=plt.hist(adv_u, 100, log=True, label="Adversarial (FGS)")
    b=plt.hist(valid_u, 100, log=True, label="Valid")
    plt.xlabel('Uncertainty')
    plt.ylabel('Occurrances (log scaled)')
    plt.legend()
    """
    fig = plt.figure(figsize=(4,3))
    fig.subplots_adjust(bottom=0.15,left=.15)
    b=plt.hist(valid_u-adv_u, 25, label="Valid")
    plt.xlabel('U(valid)-U(adversarial)')
    plt.ylabel('Occurrences')

    pp = PdfPages('results1.pdf')
    plt.savefig(pp, format='pdf')
    pp.close()
    plt.show()


##BNN.create_model equiv to make_model.. model() = average_preds... self.model_list() = actual posteriors
# test(MNISTModel, MNIST(), "models/mnist")
# test(CIFARModel, CIFAR(), "models/cifar")

if __name__ == "__main__":
    #TODO: if still want change so you pass in the path
    run_attacks()
    # run_mc_drop()

    # datasets = ["CIFAR10", "MNIST"]
    # inf_methods = ["ADVI", "NUTS", "MCDROP"]
    # colors = ["gray", "white"]
    # confs = [0,0.1,0.2,0.3,0.4,0.5,0.6,0.7,0.8,0.9,1,2,3,4,5,6,7,8,9,10]
    # for dataset in datasets:
    #     for color in colors:    
    #         for inf in inf_methods:
    #             linestyle = '-' if color == "gray" else '--'
    #             if inf == "ADVI":
    #                 c = "red"
    #             elif inf == "NUTS":
    #                 c = "blue"
    #             else:
    #                 c = "green"
    #             path = "results/{}_{}_{}".format(dataset, inf, color)
    #             title_desc = "{} {} {}box".format(dataset, inf, color)
    #             filename = "plots/{}_{}_{}".format(dataset, inf, color)
    #             aucs = []
    #             max_auc = 0
    #             plot_TPRs = []
    #             plot_FPRs = []
    #             uncs_path = path + "_uncs.npy"
    #             dists_uncs = np.load(uncs_path)
    #             for uncs in dists_uncs:
    #                 auc_val, TPRs, FPRs = roc_auc(uncs[0], uncs[1])
    #                 if auc_val > max_auc:
    #                     max_auc = auc_val
    #                     plot_TPRs = TPRs
    #                     plot_FPRs = FPRs
    #                 aucs.append(auc_val)
    #             # plt.plot(plot_FPRs + [1.0], plot_TPRs + [1.0], label = title_desc + " = {:.2f}".format(max_auc), color = c, linestyle = linestyle)
    #             # plt.title("ROC Curves")
    #             # plt.xlabel("FPR")
    #             # plt.ylabel("TPR")


    #             # dists_path = path + "_dists.npy"
    #             # dists = np.load(dists_path)
    #             # zipped = sorted(zip(dists, aucs))

    #             # plot_1_xy = ([a for a,b in zipped], [b for a,b in zipped])
    #             # plt.plot(confs, aucs, label = title_desc, color = c, linestyle = linestyle)
    #             # plt.title("AUC vs Confidence".format(title_desc))
    #             # plt.xlabel("Confidence")
    #             # plt.ylabel("AUC Value")


    #             adv_accs_path = path + "_adv_accs.npy"
    #             adv_accs = np.load(adv_accs_path)
    #             # zipped = sorted(zip(dists, adv_accs))
    #             # plot_1_xy = ([a for a,b in zipped], [b for a,b in zipped])
    #             # plt.plot(plot_1_xy[0], plot_1_xy[1], label = title_desc, color = c, linestyle = linestyle)
    #             plt.plot(confs, adv_accs, label = title_desc, color = c, linestyle = linestyle)
    #             plt.title("Adversarial Accuracy vs Confidence")
    #             plt.xlabel("Confidence")
    #             plt.ylabel("Adv Accuracy")
    #     plt.legend()
    #     plt.show()