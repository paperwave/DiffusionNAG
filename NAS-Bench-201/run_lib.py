import os
import torch
import numpy as np
import random
import logging
from absl import flags
from scipy.stats import pearsonr, spearmanr
import torch

from models import cate
from models import digcn
from models import digcn_meta
import losses
import sampling
from models import utils as mutils
from models.ema import ExponentialMovingAverage
import datasets_nas
import sde_lib
from utils import *
from logger import Logger
from analysis.arch_metrics import SamplingArchMetrics, SamplingArchMetricsMeta

FLAGS = flags.FLAGS


def set_exp_name(config):
    if config.task == 'tr_scorenet':
        exp_name = f'./results/{config.task}/{config.folder_name}'
        data = config.data

    elif config.task == 'tr_meta_surrogate':
        exp_name = f'./results/{config.task}/{config.folder_name}'

    os.makedirs(exp_name, exist_ok=True)
    config.exp_name = exp_name
    set_random_seed(config)

    return exp_name


def set_random_seed(config):
    seed = config.seed
    os.environ['PYTHONHASHSEED'] = str(seed)

    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)

    np.random.seed(seed)
    random.seed(seed)

    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False


def scorenet_train(config):
    """Runs the score network training pipeline.
    Args:
        config: Configuration to use.
    """

    ## Set logger
    exp_name = set_exp_name(config)
    logger = Logger(
        log_dir=exp_name,
        write_textfile=True)
    logger.update_config(config, is_args=True)
    logger.write_str(str(vars(config)))
    logger.write_str('-' * 100)

    ## Create directories for experimental logs
    sample_dir = os.path.join(exp_name, "samples")
    os.makedirs(sample_dir, exist_ok=True)

    ## Initialize model and optimizer
    score_model = mutils.create_model(config)
    ema = ExponentialMovingAverage(score_model.parameters(), decay=config.model.ema_rate)
    optimizer = losses.get_optimizer(config, score_model.parameters())
    state = dict(optimizer=optimizer, model=score_model, ema=ema, step=0, config=config)

    ## Create checkpoints directory
    checkpoint_dir = os.path.join(exp_name, "checkpoints")

    ## Intermediate checkpoints to resume training
    checkpoint_meta_dir = os.path.join(exp_name, "checkpoints-meta", "checkpoint.pth")
    os.makedirs(checkpoint_dir, exist_ok=True)
    os.makedirs(os.path.dirname(checkpoint_meta_dir), exist_ok=True)

    ## Resume training when intermediate checkpoints are detected
    if config.resume:
        state = restore_checkpoint(config.resume_ckpt_path, state, config.device, resume=config.resume)
    initial_step = int(state['step'])

    ## Build dataloader and iterators
    train_ds, eval_ds, test_ds = datasets_nas.get_dataset(config)
    train_loader, eval_loader, test_loader = datasets_nas.get_dataloader(config, train_ds, eval_ds, test_ds)
    train_iter = iter(train_loader)

    # Create data normalizer and its inverse
    scaler = datasets_nas.get_data_scaler(config)
    inverse_scaler = datasets_nas.get_data_inverse_scaler(config)

    ## Setup SDEs
    if config.training.sde.lower() == 'vpsde':
        sde = sde_lib.VPSDE(beta_min=config.model.beta_min, beta_max=config.model.beta_max, N=config.model.num_scales)
        sampling_eps = 1e-3
    elif config.training.sde.lower() == 'vesde':
        sde = sde_lib.VESDE(sigma_min=config.model.sigma_min, sigma_max=config.model.sigma_max, N=config.model.num_scales)
        sampling_eps = 1e-5
    else:
        raise NotImplementedError(f"SDE {config.training.sde} unknown.")

    # Build one-step training and evaluation functions
    optimize_fn = losses.optimization_manager(config)
    continuous = config.training.continuous
    reduce_mean = config.training.reduce_mean
    likelihood_weighting = config.training.likelihood_weighting
    train_step_fn = losses.get_step_fn(sde=sde,
                                       train=True, 
                                       optimize_fn=optimize_fn,
                                       reduce_mean=reduce_mean, 
                                       continuous=continuous,
                                       likelihood_weighting=likelihood_weighting,
                                       data=config.data.name)
    eval_step_fn = losses.get_step_fn(sde=sde, 
                                      train=False, 
                                      optimize_fn=optimize_fn,
                                      reduce_mean=reduce_mean, 
                                      continuous=continuous,
                                      likelihood_weighting=likelihood_weighting,
                                      data=config.data.name)

    ## Build sampling functions
    if config.training.snapshot_sampling:
        sampling_shape = (config.training.eval_batch_size, config.data.max_node, config.data.n_vocab)
        sampling_fn = sampling.get_sampling_fn(config=config, 
                                               sde=sde, 
                                               shape=sampling_shape, 
                                               inverse_scaler=inverse_scaler, 
                                               eps=sampling_eps)

    ## Build analysis tools
    sampling_metrics = SamplingArchMetrics(config, train_ds, exp_name)

    ## Start training the score network
    logging.info("Starting training loop at step %d." % (initial_step,))
    element = {'train': ['training_loss'],
                'eval': ['eval_loss'],
                'test': ['test_loss'],
                'sample': ['r_valid', 'r_unique', 'r_novel']}

    num_train_steps = config.training.n_iters
    is_best = False
    min_test_loss = 1e05
    for step in range(initial_step, num_train_steps+1):
        try:
            x, adj, extra = next(train_iter)
        except StopIteration:
            train_iter = train_loader.__iter__()
            x, adj, extra = next(train_iter)
        mask = aug_mask(adj, algo=config.data.aug_mask_algo, data=config.data.name)
        x, adj, mask = scaler(x.to(config.device)), adj.to(config.device), mask.to(config.device)
        batch = (x, adj, mask)

        ## Execute one training step
        loss = train_step_fn(state, batch)
        logger.update(key="training_loss", v=loss.item())
        if step % config.training.log_freq == 0:
            logging.info("step: %d, training_loss: %.5e" % (step, loss.item()))

        ## Report the loss on evaluation dataset periodically
        if step % config.training.eval_freq == 0:
            for eval_x, eval_adj, eval_extra in eval_loader:
                eval_mask = aug_mask(eval_adj, algo=config.data.aug_mask_algo, data=config.data.name)
                eval_x, eval_adj, eval_mask = scaler(eval_x.to(config.device)), eval_adj.to(config.device), eval_mask.to(config.device)
                eval_batch = (eval_x, eval_adj, eval_mask)
                eval_loss = eval_step_fn(state, eval_batch)
                logging.info("step: %d, eval_loss: %.5e" % (step, eval_loss.item()))
                logger.update(key="eval_loss", v=eval_loss.item())
            for test_x, test_adj, test_extra in test_loader:
                test_mask = aug_mask(test_adj, algo=config.data.aug_mask_algo, data=config.data.name)
                test_x, test_adj, test_mask = scaler(test_x.to(config.device)), test_adj.to(config.device), test_mask.to(config.device)
                test_batch = (test_x, test_adj, test_mask)
                test_loss = eval_step_fn(state, test_batch)
                logging.info("step: %d, test_loss: %.5e" % (step, test_loss.item()))
                logger.update(key="test_loss", v=test_loss.item())
            if logger.logs['test_loss'].avg < min_test_loss:
                is_best = True

        ## Save the checkpoint
        if step != 0 and step % config.training.snapshot_freq == 0 or step == num_train_steps:
            save_step = step // config.training.snapshot_freq
            save_checkpoint(checkpoint_dir, state, step, save_step, is_best)

            ## Generate samples
            if config.training.snapshot_sampling:
                ema.store(score_model.parameters())
                ema.copy_to(score_model.parameters())
                sample, sample_steps, _ = sampling_fn(score_model, mask)
                quantized_sample = quantize(sample)
                this_sample_dir = os.path.join(sample_dir, "iter_{}".format(step))
                os.makedirs(this_sample_dir, exist_ok=True)

                ## Evaluate samples
                arch_metric = sampling_metrics(arch_list=quantized_sample, this_sample_dir=this_sample_dir)
                r_valid, r_unique, r_novel = arch_metric[0][0], arch_metric[0][1],  arch_metric[0][2]
                logger.update(key="r_valid", v=r_valid)
                logger.update(key="r_unique", v=r_unique)
                logger.update(key="r_novel", v=r_novel)
                logging.info("r_valid: %.5e" % (r_valid))
                logging.info("r_unique: %.5e" % (r_unique))
                logging.info("r_novel: %.5e" % (r_novel))

        if step % config.training.eval_freq == 0:
            logger.write_log(element=element, step=step)
        else:
            logger.write_log(element={'train': ['training_loss']}, step=step)

        logger.reset()

    logger.save_log()


def scorenet_evaluate(config):
    """Evaluate trained score network.
    Args:
        config: Configuration to use.
    """

    ## Set logger
    exp_name = set_exp_name(config)
    logger = Logger(
        log_dir=exp_name,
        write_textfile=True)
    logger.update_config(config, is_args=True)
    logger.write_str(str(vars(config)))
    logger.write_str('-' * 100)

    ## Load the config of pre-trained score network
    score_config = torch.load(config.scorenet_ckpt_path)['config']

    ## Setup SDEs
    if score_config.training.sde.lower() == 'vpsde':
        sde = sde_lib.VPSDE(beta_min=score_config.model.beta_min, beta_max=score_config.model.beta_max, N=score_config.model.num_scales)
        sampling_eps = 1e-3
    elif score_config.training.sde.lower() == 'vesde':
        sde = sde_lib.VESDE(sigma_min=score_config.model.sigma_min, sigma_max=score_config.model.sigma_max, N=score_config.model.num_scales)
        sampling_eps = 1e-5
    else:
        raise NotImplementedError(f"SDE {config.training.sde} unknown.")

    ## Creat data normalizer and its inverse
    inverse_scaler = datasets_nas.get_data_inverse_scaler(config)

    # Build the sampling function
    sampling_shape = (config.eval.batch_size, score_config.data.max_node, score_config.data.n_vocab)
    sampling_fn = sampling.get_sampling_fn(config=config, 
                                           sde=sde, 
                                           shape=sampling_shape, 
                                           inverse_scaler=inverse_scaler, 
                                           eps=sampling_eps)

    ## Load pre-trained score network
    score_model = mutils.create_model(score_config)
    ema = ExponentialMovingAverage(score_model.parameters(), decay=score_config.model.ema_rate)
    state = dict(model=score_model, ema=ema, step=0, config=score_config)
    state = restore_checkpoint(config.scorenet_ckpt_path, state, device=config.device, resume=True)
    ema.store(score_model.parameters())
    ema.copy_to(score_model.parameters())

    ## Build dataset
    train_ds, eval_ds, test_ds = datasets_nas.get_dataset(score_config)

    ## Build analysis tools
    sampling_metrics = SamplingArchMetrics(config, train_ds, exp_name)

    ## Create directories for experimental logs
    sample_dir = os.path.join(exp_name, "samples")
    os.makedirs(sample_dir, exist_ok=True)

    ## Start sampling
    logging.info("Starting sampling")
    element = {'sample': ['r_valid', 'r_unique', 'r_novel']}

    num_sampling_rounds = int(np.ceil(config.eval.num_samples / config.eval.batch_size))
    print(f'>>> Sampling for {num_sampling_rounds} rounds...')

    all_samples = []
    adj = train_ds.adj.to(config.device)
    mask = train_ds.mask(algo=score_config.data.aug_mask_algo).to(config.device)
    if len(adj.shape) == 2: adj = adj.unsqueeze(0)
    if len(mask.shape) == 2: mask = mask.unsqueeze(0)

    for _ in range(num_sampling_rounds):
        sample, sample_steps, _ = sampling_fn(score_model, mask)
        quantized_sample = quantize(sample)
        all_samples += quantized_sample

    ## Evaluate samples
    all_samples = all_samples[:config.eval.num_samples]
    arch_metric = sampling_metrics(arch_list=all_samples, this_sample_dir=sample_dir)
    r_valid, r_unique, r_novel = arch_metric[0][0], arch_metric[0][1],  arch_metric[0][2]
    logger.update(key="r_valid", v=r_valid)
    logger.update(key="r_unique", v=r_unique)
    logger.update(key="r_novel", v=r_novel)
    logger.write_log(element=element, step=1)
    logger.save_log()


def meta_surrogate_train(config):
    """Runs the meta-predictor model training pipeline.
    Args:
        config: Configuration to use.
    """
    ## Set logger
    exp_name = set_exp_name(config)
    logger = Logger(
        log_dir=exp_name,
        write_textfile=True)
    logger.update_config(config, is_args=True)
    logger.write_str(str(vars(config)))
    logger.write_str('-' * 100)

    ## Create directories for experimental logs
    sample_dir = os.path.join(exp_name, "samples")
    os.makedirs(sample_dir, exist_ok=True)

    ## Initialize model and optimizer
    surrogate_model = mutils.create_model(config)
    optimizer = losses.get_optimizer(config, surrogate_model.parameters())
    state = dict(optimizer=optimizer, model=surrogate_model, step=0, config=config)

    ## Create checkpoints directory
    checkpoint_dir = os.path.join(exp_name, "checkpoints")

    ## Intermediate checkpoints to resume training
    checkpoint_meta_dir = os.path.join(exp_name, "checkpoints-meta", "checkpoint.pth")
    os.makedirs(checkpoint_dir, exist_ok=True)
    os.makedirs(os.path.dirname(checkpoint_meta_dir), exist_ok=True)

    ## Resume training when intermediate checkpoints are detected and resume=True
    state = restore_checkpoint(checkpoint_meta_dir, state, config.device, resume=config.resume)
    initial_step = int(state['step'])

    ## Build dataloader and iterators
    train_ds, eval_ds, test_ds = datasets_nas.get_meta_dataset(config)
    train_loader, eval_loader, _ = datasets_nas.get_dataloader(config, train_ds, eval_ds, test_ds)
    train_iter = iter(train_loader)

    ## Create data normalizer and its inverse
    scaler = datasets_nas.get_data_scaler(config)
    inverse_scaler = datasets_nas.get_data_inverse_scaler(config)

    ## Setup SDEs
    if config.training.sde.lower() == 'vpsde':
        sde = sde_lib.VPSDE(beta_min=config.model.beta_min, beta_max=config.model.beta_max, N=config.model.num_scales)
        sampling_eps = 1e-3
    elif config.training.sde.lower() == 'vesde':
        sde = sde_lib.VESDE(sigma_min=config.model.sigma_min, sigma_max=config.model.sigma_max, N=config.model.num_scales)
        sampling_eps = 1e-5
    else:
        raise NotImplementedError(f"SDE {config.training.sde} unknown.")

    ## Build one-step training and evaluation functions
    optimize_fn = losses.optimization_manager(config)
    continuous = config.training.continuous
    reduce_mean = config.training.reduce_mean
    likelihood_weighting = config.training.likelihood_weighting
    train_step_fn = losses.get_step_fn_predictor(sde=sde, 
                                                 train=True, 
                                                 optimize_fn=optimize_fn,
                                                 reduce_mean=reduce_mean, 
                                                 continuous=continuous,
                                                 likelihood_weighting=likelihood_weighting,
                                                 data=config.data.name, 
                                                 label_list=config.data.label_list, 
                                                 noised=config.training.noised)
    eval_step_fn = losses.get_step_fn_predictor(sde,
                                                train=False, 
                                                optimize_fn=optimize_fn,
                                                reduce_mean=reduce_mean, 
                                                continuous=continuous,
                                                likelihood_weighting=likelihood_weighting,
                                                data=config.data.name, 
                                                label_list=config.data.label_list, 
                                                noised=config.training.noised)

    ## Build sampling functions
    if config.training.snapshot_sampling:
        sampling_shape = (config.training.eval_batch_size, config.data.max_node, config.data.n_vocab)
        sampling_fn = sampling.get_sampling_fn(config=config, 
                                               sde=sde, 
                                               shape=sampling_shape, 
                                               inverse_scaler=inverse_scaler, 
                                               eps=sampling_eps, 
                                               conditional=True, 
                                               data_name=config.sampling.check_dataname, # for sanity check
                                               num_sample=config.model.num_sample)
        ## Load pre-trained score network
        score_config = torch.load(config.scorenet_ckpt_path)['config']
        check_config(score_config, config)
        score_model = mutils.create_model(score_config)
        score_ema = ExponentialMovingAverage(score_model.parameters(), decay=score_config.model.ema_rate)
        score_state = dict(model=score_model, ema=score_ema, step=0, config=score_config)
        score_state = restore_checkpoint(config.scorenet_ckpt_path, score_state, device=config.device, resume=True)
        score_ema.copy_to(score_model.parameters())

    ## Build analysis tools
    sampling_metrics = SamplingArchMetricsMeta(config, train_ds, exp_name)

    ## Start training
    logging.info("Starting training loop at step %d." % (initial_step,))
    element = {'train': ['training_loss'],
                'eval': ['eval_loss', 'eval_p_corr', 'eval_s_corr'],
                'sample': ['r_valid', 'r_unique', 'r_novel']}
    num_train_steps = config.training.n_iters
    is_best = False
    max_eval_p_corr = -1
    for step in range(initial_step, num_train_steps + 1):
        try:
            x, adj, extra, task = next(train_iter)
        except StopIteration:
            train_iter = train_loader.__iter__()
            x, adj, extra, task = next(train_iter)
        mask = aug_mask(adj, algo=config.data.aug_mask_algo, data=config.data.name)
        x, adj, mask, task = scaler(x.to(config.device)), adj.to(config.device), mask.to(config.device), task.to(config.device)
        batch = (x, adj, mask, extra, task)

        ## Execute one training step
        loss, pred, labels = train_step_fn(state, batch)
        logger.update(key="training_loss", v=loss.item())
        if step % config.training.log_freq == 0:
            logging.info("step: %d, training_loss: %.5e" % (step, loss.item()))

        ## Report the loss on evaluation dataset periodically
        if step % config.training.eval_freq == 0:
            eval_pred_list, eval_labels_list = list(), list()
            for eval_x, eval_adj, eval_extra, eval_task in eval_loader:
                eval_mask = aug_mask(eval_adj, algo=config.data.aug_mask_algo, data=config.data.name)
                eval_x, eval_adj, eval_mask, eval_task = scaler(eval_x.to(config.device)), eval_adj.to(config.device), eval_mask.to(config.device), eval_task.to(config.device)
                eval_batch = (eval_x, eval_adj, eval_mask, eval_extra, eval_task)
                eval_loss, eval_pred, eval_labels = eval_step_fn(state, eval_batch)
                eval_pred_list += [v.detach().item() for v in eval_pred.squeeze()]
                eval_labels_list += [v.detach().item() for v in eval_labels.squeeze()]
                logging.info("step: %d, eval_loss: %.5e" % (step, eval_loss.item()))
                logger.update(key="eval_loss", v=eval_loss.item())
            eval_p_corr = pearsonr(np.array(eval_pred_list), np.array(eval_labels_list))[0]
            eval_s_corr = spearmanr(np.array(eval_pred_list), np.array(eval_labels_list))[0]
            logging.info("step: %d, eval_p_corr: %.5e" % (step, eval_p_corr))
            logging.info("step: %d, eval_s_corr: %.5e" % (step, eval_s_corr))
            logger.update(key="eval_p_corr", v=eval_p_corr)
            logger.update(key="eval_s_corr", v=eval_s_corr)
            if eval_p_corr > max_eval_p_corr:
                is_best = True
                max_eval_p_corr = eval_p_corr

        ## Save a checkpoint periodically and generate samples
        if step != 0 and step % config.training.snapshot_freq == 0 or step == num_train_steps:
            ## Save the checkpoint.
            save_step = step // config.training.snapshot_freq
            save_checkpoint(checkpoint_dir, state, step, save_step, is_best)
            ## Generate and save samples
            if config.training.snapshot_sampling:
                score_ema.store(score_model.parameters())
                score_ema.copy_to(score_model.parameters())
                sample = sampling_fn(score_model=score_model, 
                                     mask=mask, 
                                     classifier=surrogate_model,
                                     classifier_scale=config.sampling.classifier_scale)
                quantized_sample = quantize(sample) # quantization
                this_sample_dir = os.path.join(sample_dir, "iter_{}".format(step))
                os.makedirs(this_sample_dir, exist_ok=True)
                ## Evaluate samples
                arch_metric = sampling_metrics(arch_list=quantized_sample,
                                                this_sample_dir=this_sample_dir,
                                                check_dataname=config.sampling.check_dataname)
                r_valid, r_unique, r_novel = arch_metric[0][0], arch_metric[0][1],  arch_metric[0][2]
                logging.info("step: %d, r_valid: %.5e" % (step, r_valid))
                logging.info("step: %d, r_unique: %.5e" % (step, r_unique))
                logging.info("step: %d, r_novel: %.5e" % (step, r_novel))
                logger.update(key="r_valid", v=r_valid)
                logger.update(key="r_unique", v=r_unique)
                logger.update(key="r_novel", v=r_novel)

        if step % config.training.eval_freq == 0:
            logger.write_log(element=element, step=step)
        else:
            logger.write_log(element={'train': ['training_loss']}, step=step)

        logger.reset()


def check_config(config1, config2):
    assert config1.model.sigma_min == config2.model.sigma_min
    assert config1.model.sigma_max == config2.model.sigma_max
    assert config1.training.sde == config2.training.sde
    assert config1.training.continuous == config2.training.continuous
    assert config1.data.centered == config2.data.centered
    assert config1.data.max_node == config2.data.max_node
    assert config1.data.n_vocab == config2.data.n_vocab


run_train_dict = {
    'scorenet': scorenet_train,
    'meta_surrogate': meta_surrogate_train
}


run_eval_dict = {
    'scorenet': scorenet_evaluate,
}


def train(config):
    run_train_dict[config.model_type](config)


def evaluate(config):
    run_eval_dict[config.model_type](config)

