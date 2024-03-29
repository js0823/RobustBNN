import pymc3 as pm
import theano
import pickle
import os
import numpy as np
floatX = theano.config.floatX
from scipy.stats import mode
from theano.misc.pkl_utils import load, dump

'''
def save_trace(trace, filename):
	with open(filename, 'wb') as buff:
		pickle.dump({'trace': trace}, buff)
	print("Saving trace done.")

def load_trace(filename):
	with open(filename, 'rb') as buff:
		data = pickle.load(buff)
	trace = data['trace']
	print("Loading trace done.")
	return trace
'''

# Save trace and load that works with gpu/cpu conversion.
# TODO: When trace is loaded, it results in very bad accuracy. Why?
def save_trace(trace, filename):
	with open(filename, 'wb') as buff:
		dump({'trace': trace}, buff)
	print("Saving trace done.")

def load_trace(filename):
	trace = []
	with open(filename, 'rb') as buff:
		#trace = load(buff)['trace'] # this works on gpu but why not on cpu...
		npzfile = np.load(buff)
		for t in npzfile:
			trace.append(np.asarray(t))
	print(type(trace))
	print("Loading trace done.")
	return trace

def train_model(inference_alg, model, num_posterior, nn_input, nn_output, X_train, Y_train, X_test, Y_test):
	#inference_alg.fit(n, method, data)
	#return posterior_samples

	if inference_alg is 'advi':
		minibatch_x = pm.Minibatch(X_train.astype(floatX), batch_size=500)
		minibatch_y = pm.Minibatch(Y_train.astype(floatX), batch_size=500)
		with model:
			# TODO: NaN Problem when increasing hidden nodes.
			# https://discourse.pymc.io/t/nan-occurred-in-optimization-with-advi/1089
			inference = pm.ADVI()
			approx = pm.fit(n=150000, method=inference,
								more_replacements={nn_input:minibatch_x, nn_output:minibatch_y})
			trace = approx.sample(draws=num_posterior)
		
		print(pm.summary(trace))

		nn_input.set_value(X_test)
		nn_output.set_value(Y_test)

		with model:
			ppc_test = pm.sample_ppc(trace, samples=num_posterior)
			pred_test = mode(ppc_test['out'], axis=0).mode[0, :]

	elif inference_alg is 'nuts':
		with model:
			#sample_kwargs = {'cores': 1, 'init': 'advi+adapt_diag', 
			#					'draws': num_posterior, 'max_treedepth': 15, 'target_accept': 0.9}
			sample_kwargs = {'cores': 1, 'init': 'auto'}
			trace = pm.sample(**sample_kwargs)
		
		print(pm.summary(trace))

		nn_input.set_value(X_test)
		nn_output.set_value(Y_test)

		with model:
			ppc_test = pm.sample_ppc(trace, samples=num_posterior)
			pred_test = mode(ppc_test['out'], axis=0).mode[0, :]
	
	elif inference_alg is 'hmc':
		with model:
			step = pm.HamiltonianMC(step_scale=0.15)
			sample_kwargs = {'step': step, 'draws': num_posterior}
			trace = pm.sample(**sample_kwargs)
		
		pm.summary(trace)

		nn_input.set_value(X_test)
		nn_output.set_value(Y_test)

		with model:
			ppc_test = pm.sample_ppc(trace, samples=num_posterior)
			pred_test = mode(ppc_test['out'], axis=0).mode[0, :]
	
	return pred_test, trace

def eval_pickled_model(model, num_posterior, nn_input, nn_output, X_test, Y_test, trace=None):
	nn_input.set_value(X_test)
	nn_output.set_value(Y_test)

	with model:
		ppc_test = pm.sample_ppc(trace, samples=num_posterior)
		pred_test = mode(ppc_test['out'], axis=0).mode[0, :]
	
	return pred_test