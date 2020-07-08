import torch
from torch.distributions import transform_to
import argparse
import subprocess
import datetime
import pickle
import time
import os
from functools import partial
from contextlib import ExitStack
import logging

import pyro
import pyro.optim as optim
import pyro.distributions as dist
from pyro.contrib.util import iter_plates_to_shape, rexpand, rmv
from pyro.contrib.oed.eig import marginal_eig, elbo_learn, nmc_eig, pce_eig
import pyro.contrib.gp as gp
from pyro.contrib.oed.differentiable_eig import _differentiable_posterior_loss, differentiable_pce_eig, _differentiable_ace_eig_loss
from pyro.contrib.oed.eig import opt_eig_ape_loss
from pyro.util import is_bad

from ces_gradients import PosteriorGuide, LinearPosteriorGuide


# TODO read from torch float spec
epsilon = torch.tensor(2**-22)


def get_git_revision_hash():
    return subprocess.check_output(['git', 'rev-parse', 'HEAD'])

def make_ces_model(rho_concentration, alpha_concentration, slope_mu, slope_sigma, observation_sd, observation_label="y"):
    def ces_model(design):
        if is_bad(design):
            raise ArithmeticError("bad design, contains nan or inf")
        batch_shape = design.shape[:-2]
        with ExitStack() as stack:
            for plate in iter_plates_to_shape(batch_shape):
                stack.enter_context(plate)
            rho_shape = batch_shape + (rho_concentration.shape[-1],)
            rho = 0.01 + 0.99 * pyro.sample("rho", dist.Dirichlet(rho_concentration.expand(rho_shape))).select(-1, 0)
            alpha_shape = batch_shape + (alpha_concentration.shape[-1],)
            alpha = pyro.sample("alpha", dist.Dirichlet(alpha_concentration.expand(alpha_shape)))
            slope = pyro.sample("slope", dist.LogNormal(slope_mu.expand(batch_shape), slope_sigma.expand(batch_shape)))
            rho, slope = rexpand(rho, design.shape[-2]), rexpand(slope, design.shape[-2])
            d1, d2 = design[..., 0:3], design[..., 3:6]
            U1rho = (rmv(d1.pow(rho.unsqueeze(-1)), alpha)).pow(1./rho)
            U2rho = (rmv(d2.pow(rho.unsqueeze(-1)), alpha)).pow(1./rho)
            mean = slope * (U1rho - U2rho)
            sd = slope * observation_sd * (1 + torch.norm(d1 - d2, dim=-1, p=2))

            logging.debug('rho max {} min {}'.format(rho.max().item(), rho.min().item()))
            logging.debug('latent samples: rho {} alpha {} slope mean {} slope median {}'.format(
                rho.mean().item(), alpha.mean().item(), slope.mean().item(), slope.median().item()))
            logging.debug('mean: mean {} sd {} min {} max {}'.format(
                mean.mean().item(), mean.std().item(), mean.min().item(), mean.max().item()))
            logging.debug('sd: mean {}, sd {}, min {}, max {}'.format(sd.mean(), sd.std(), sd.min(), sd.max()))

            emission_dist = dist.CensoredSigmoidNormal(mean, sd, 1 - epsilon, epsilon).to_event(1)
            y = pyro.sample(observation_label, emission_dist)
            return y

    return ces_model

#Creates model with predefined fixed theta (w and sigma), outputs y as a regression outcome
def make_regression_model(w_loc, w_scale, sigma_scale, observation_label="y"):
    def regression_model(design):

        if is_bad(design):
            raise ArithmeticError("bad design, contains nan or inf")
        batch_shape = design.shape[:-2]
        with pyro.plate_stack("plate_stack", batch_shape):
            # `w` is shape p, the prior on each component is independent
            w = pyro.sample("w", dist.Laplace(w_loc, w_scale).to_event(1))
            # `sigma` is scalar
            sigma = 1e-6 + pyro.sample("sigma", dist.Exponential(sigma_scale)).unsqueeze(-1)
            mean = rmv(design, w)
            sd = sigma
            y = pyro.sample(observation_label, dist.Normal(mean, sd).to_event(1))
            return y

    return regression_model


def make_learn_xi_model(model):
    def model_learn_xi(design_prototype):
        design = pyro.param("xi")
        design = design.expand(design_prototype.shape)
        return model(design)
    return model_learn_xi


def elboguide(design, n, p):

    w_loc = pyro.param("w_loc", torch.ones(p))
    w_scale = pyro.param("w_scale", torch.ones(p),
                                     constraint=torch.distributions.constraints.positive)
    sigma_loc = pyro.param("sigma_loc", torch.ones(1))
    sigma_scale = pyro.param("sigma_scale", torch.ones(1),
                                     constraint=torch.distributions.constraints.positive)
    batch_shape = design.shape[:-2]
    with ExitStack() as stack:
        for plate in iter_plates_to_shape(batch_shape):
            stack.enter_context(plate)
        w_shape = batch_shape + (w_loc.shape[-1],)
        pyro.sample("w", dist.Normal(w_loc.expand(w_shape), w_scale.expand(w_shape)).to_event(1))
        pyro.sample("sigma", dist.Normal(sigma_loc, sigma_scale)).unsqueeze(-1)



def neg_loss(loss):
    def new_loss(*args, **kwargs):
        return (-a for a in loss(*args, **kwargs))
    return new_loss

# HELP what is loglevel, num_acquisition etc
# Creates rollout with initial fixed parameters, true values of theta fixed ?
# HELP what does function do, numsteps, num_parallel ?
def main(num_steps, num_parallel, experiment_name, typs, seed, num_gradient_steps, num_samples,
         num_contrast_samples, loglevel, n, p, scale):
    numeric_level = getattr(logging, loglevel.upper(), None)
    if not isinstance(numeric_level, int):
        raise ValueError("Invalid log level: {}".format(loglevel))
    logging.basicConfig(level=numeric_level)

    output_dir = "./run_outputs/ces/"
    if not experiment_name:
        experiment_name = output_dir+"{}".format(datetime.datetime.now().isoformat())
    else:
        experiment_name = output_dir+experiment_name
    results_file = experiment_name + '.result_stream.pickle'
    try:
        os.remove(results_file)
    except OSError:
        logging.info("File {} does not exist yet".format(results_file))
    typs = typs.split(",")

    for typ in typs:
        logging.info("Type {}".format(typ))
        pyro.clear_param_store()
        if seed >= 0:
            pyro.set_rng_seed(seed)
        else:
            seed = int(torch.rand(tuple()) * 2**30)
            pyro.set_rng_seed(seed)


        # Change the prior distribution here
        # prior params
        w_loc = torch.zeros(p)
        w_scale = scale * torch.ones(p)
        sigma_scale = scale * torch.tensor(1.)

        true_model = pyro.condition(make_regression_model(
            w_loc, w_scale, sigma_scale),
                                    {"w": torch.tensor([0.,2.,3.,4.,5.,6.]), "sigma": torch.tensor(.5)})

        prior = make_regression_model(w_loc.clone(), w_scale.clone(), sigma_scale.clone())

        elbo_n_samples, elbo_n_steps, elbo_lr = 10, 1000, 0.04
#HELP what are contrastive_samples
        contrastive_samples = num_samples
        targets = ["w", "sigma"]

        d_star_designs = torch.tensor([])
        ys = torch.tensor([])

        results = {'typ': typ, 'step': [], 'git-hash': get_git_revision_hash(), 'seed': seed,
                   'num_gradient_steps': num_gradient_steps, 'num_samples': num_samples,
                   'num_contrast_samples': num_contrast_samples, 'design_time': [], 'd_star_design': [],
                   'y': [], 'w_loc': [], 'w_scale': [], 'sigma_scale': []}

        for step in range(num_steps):
            logging.info("Step {}".format(step))
            model = make_regression_model(w_loc, w_scale, sigma_scale)

            # Design phase
            t = time.time()
            results['step'].append(step)
            if typ in ['posterior-grad', 'pce-grad', 'ace-grad']:
                model_learn_xi = make_learn_xi_model(model)
                grad_start_lr, grad_end_lr = 0.001, 0.001

                if typ == 'pce-grad':

                    # Suggested num_gradient_steps = 2500
                    eig_loss = lambda d, N, **kwargs: pce_eig(
                        model=model_learn_xi, design=d, observation_labels=["y"], target_labels=targets,
                        N=N, M=contrastive_samples, **kwargs)
                    loss = neg_loss(eig_loss)

                xi_init = 20 * torch.rand((num_parallel, n, p)) - 10
                pyro.param("xi", xi_init)
                pyro.get_param_store().replace_param("xi", xi_init, pyro.param("xi"))
                design_prototype = torch.zeros((num_parallel, n, p))  # this is annoying, code needs refactor

                start_lr, end_lr = grad_start_lr, grad_end_lr
                gamma = (end_lr / start_lr) ** (1 / num_gradient_steps)
                scheduler = pyro.optim.ExponentialLR({'optimizer': torch.optim.Adam, 'optim_args': {'lr': start_lr},
                                                      'gamma': gamma})
                print(design_prototype.shape)
                ape = opt_eig_ape_loss(design_prototype, loss, num_samples=num_samples, num_steps=num_gradient_steps,
                                       optim=scheduler, final_num_samples=500)
                d_star_design = pyro.param("xi").detach().clone()
                print(d_star_design.shape)

            elapsed = time.time() - t
            logging.info('elapsed design time {}'.format(elapsed))
            results['design_time'].append(elapsed)
            results['d_star_design'].append(d_star_design)
            logging.info('design {} {}'.format(d_star_design.squeeze(), d_star_design.shape))
            d_star_designs = torch.cat([d_star_designs, d_star_design], dim=-2)
            y = true_model(d_star_design)
            ys = torch.cat([ys, y], dim=-1)
            logging.info('ys {} {}'.format(ys.squeeze(), ys.shape))
            results['y'].append(y)
            elbo_learn(
                prior, d_star_designs, ["y"], ["w", "sigma"], elbo_n_samples, elbo_n_steps,
                partial(elboguide, n=n, p=p), {"y": ys}, optim.Adam({"lr": elbo_lr})
            )
            w_loc = pyro.param("w_loc").detach().data.clone()
            w_scale = pyro.param("w_scale").detach().data.clone()
            sigma_loc = pyro.param("sigma_loc").detach().data.clone()
            sigma_scale = pyro.param("sigma_scale").detach().data.clone()
            logging.info("w_loc {} \n w_scale {} \n sigma_loc {} \n sigma_scale {}".format(
                w_loc.squeeze(), w_scale.squeeze(), sigma_loc.squeeze(), sigma_scale.squeeze()))
            results['w_loc'].append(w_loc)
            results['w_scale'].append(w_scale)
            results['sigma_scale'].append(sigma_scale)

        with open(results_file, 'ab') as f:
            pickle.dump(results, f)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="CES (Constant Elasticity of Substitution) indifference"
                                                 " iterated experiment design")
    parser.add_argument("--num-steps", nargs="?", default=20, type=int) #num iterations
    parser.add_argument("--num-parallel", nargs="?", default=10, type=int) #batch size
    parser.add_argument("--name", nargs="?", default="", type=str)
    parser.add_argument("--typs", nargs="?", default="pce-grad", type=str)
    parser.add_argument("--seed", nargs="?", default=-1, type=int)
    parser.add_argument("--loglevel", default="info", type=str)
    parser.add_argument("--num-gradient-steps", default=1000, type=int) #gradient for convergence of svi to have good variational parameters and
    parser.add_argument("--num-samples", default=10, type=int)
    parser.add_argument("--num-contrast-samples", default=10, type=int)
    parser.add_argument("-n", default=2, type=int)
    parser.add_argument("-p", default=6, type=int)
    parser.add_argument("--scale", default=1., type=float)
    args = parser.parse_args()
    main(args.num_steps, args.num_parallel, args.name, args.typs, args.seed,
         args.num_gradient_steps, args.num_samples, args.num_contrast_samples,
         args.loglevel, args.n, args.p, args.scale)
