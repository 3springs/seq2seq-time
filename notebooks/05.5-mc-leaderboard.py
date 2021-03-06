# -*- coding: utf-8 -*-
# ---
# jupyter:
#   jupytext:
#     formats: ipynb,py:light
#     text_representation:
#       extension: .py
#       format_name: light
#       format_version: '1.5'
#       jupytext_version: 1.6.0
#   kernelspec:
#     display_name: seq2seq-time
#     language: python
#     name: seq2seq-time
# ---

# # Sequence to Sequence Models for Timeseries Regression
#
#
# In this notebook we are going to tackle a harder problem: 
# - predicting the future on a timeseries
# - by outputing sequence of predictions
# - with rough uncertainty (uncalibrated)
# - using forecasted information (like weather report, week, or cycle of the moon)
#
# Not many papers benchmark movels for multivariate regression, much less seq prediction with uncertainty. So this notebook will try a range of models on a range of dataset.
#
# We do this using a sequence to sqequence interface
#
# <img src="../reports/figures/Seq2Seq for regression.png" />
#

# - [ ] don't overfit
#     - bejing data has a problem
#     - Current?
# - [ ] make overlap between past and future?
# - [ ] do n=5 runs

# OPTIONAL: Load the "autoreload" extension so that code can change. But blacklist large modules
# %load_ext autoreload
# %autoreload 2
# %aimport -pandas
# %aimport -torch
# %aimport -numpy
# %aimport -matplotlib
# %aimport -dask
# %aimport -tqdm
# %matplotlib inline

# +
# Imports
import torch
from torch import nn, optim
from torch.nn import functional as F
from torch.autograd import Variable
import torch
import torch.utils.data

import xarray as xr
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt

from pathlib import Path
from tqdm.auto import tqdm

import pytorch_lightning as pl
# +
import holoviews as hv
from holoviews import opts
from holoviews.operation.datashader import datashade, dynspread
hv.extension('bokeh', inline=True)
from seq2seq_time.visualization.hv_ggplot import ggplot_theme
hv.renderer('bokeh').theme = ggplot_theme

# holoview datashader timeseries options
# %opts RGB [width=800 height=200 show_grid=True active_tools=["xwheel_zoom"] default_tools=["xpan","xwheel_zoom", "reset", "hover"] toolbar="right"]
# %opts Curve [width=800 height=200 show_grid=True active_tools=["xwheel_zoom"] default_tools=["xpan","xwheel_zoom", "reset", "hover"] toolbar="right"]
# %opts Scatter [width=800 height=200 show_grid=True active_tools=["xwheel_zoom"] default_tools=["xpan","xwheel_zoom", "reset", "hover"] toolbar="right"]
# %opts Layout [width=800 height=200]
# -


from seq2seq_time.data.dataset import Seq2SeqDataSet, Seq2SeqDataSets
from seq2seq_time.predict import predict, predict_multi
from seq2seq_time.util import dset_to_nc

# +
import logging
import warnings
import seq2seq_time.silence 
warnings.simplefilter('once')
warnings.simplefilter(action='ignore', category=FutureWarning)
warnings.simplefilter(action='ignore', category=DeprecationWarning)
warnings.filterwarnings('ignore', 'Consider increasing the value of the `num_workers` argument', UserWarning)
warnings.filterwarnings('ignore', 'Your val_dataloader has `shuffle=True`', UserWarning)

from pytorch_lightning import _logger as log
log.setLevel(logging.WARN)
# -

# ## Parameters

# +
device = "cuda" if torch.cuda.is_available() else "cpu"
print(f'using {device}')

timestamp = pd.Timestamp.now().strftime("%Y%m%d-%H%M%S")
print(timestamp)
window_past = 48*2
window_future = 48
batch_size = 64
num_workers = 5
datasets_root = Path('../data/processed/')
window_past
# -



# ## Plot helpers

# +
def hv_plot_std(d: xr.Dataset):
    """Plot predictions 2 standard deviations."""
    xf = d.t_target
    yp = d.y_pred
    s = d.y_pred_std
    return hv.Spread((xf, yp, s * 2),
                     label='2*std').opts(alpha=0.5, line_width=0)

def hv_plot_pred(d: xr.Dataset):
    """Plot prediction mean"""
    xf = d.t_target
    yp = d.y_pred
    return hv.Curve({'x': xf, 'y': yp})

def hv_plot_true(d: xr.Dataset):
    """Plot true past and future data seperated by red line."""
    # Plot true
    x = np.concatenate([d.t_past, d.t_target])
    yt = np.concatenate([d.y_past, d.y_true])
    p = hv.Scatter({
        'x': x,
        'y': yt
    }, label='true').opts(color='black')

    
    # plot a red line for now
    now=pd.Timestamp(d.t_source.squeeze().values)        
    p *= hv.VLine(now, label='now').opts(color='red', framewise=True)
    
    p = p.opts(
        ylabel=str(ds_preds.attrs['targets']),
        xlabel=f'{now}'
    )

    return p

def hv_plot_prediction(d: xr.Dataset) -> hv.Layout:
    """Plot a prediction into the future, at a single point in time.""" 
    p = hv_plot_true(d)
    p *= hv_plot_pred(d)
    p *= hv_plot_std(d)
    return p


# +
def plot_performance(ds_preds, full=False):
    """Multiple plots using xr_preds"""
    p = hv_plot_prediction(ds_preds.isel(t_source=10))
    display(p)

    n = len(ds_preds.t_source)
    d_ahead = ds_preds.mean(['t_source'])['nll'].groupby('t_ahead_hours').mean()
    nll_vs_tahead = (hv.Curve(
        (d_ahead.t_ahead_hours,
         d_ahead)).redim(x='hours ahead',
                         y='nll').opts(
                                       title=f'NLL vs time ahead (no. samples={n})'))
    display(nll_vs_tahead)

    # Make a plot of the NLL over time. Does this solution get worse with time?
    if full:
        d_source = ds_preds.mean(['t_ahead'])['nll'].groupby('t_source').mean()
        nll_vs_time = (hv.Curve(d_source).opts(
                                               title='Error vs time of prediction'))
        display(nll_vs_time)

    # A scatter plot is easy with xarray
    if full:
        tlim = (ds_preds.y_true.min().item(), ds_preds.y_true.max().item())
        true_vs_pred = datashade(hv.Scatter(
            (ds_preds.y_true,
             ds_preds.y_pred))).redim(x='true', y='pred').opts(width=400,
                                                               height=400,
                                                               xlim=tlim,
                                                               ylim=tlim,
                                                               title='Scatter plot')
        true_vs_pred = dynspread(true_vs_pred)
        true_vs_pred
        display(true_vs_pred)
        
def read_hist(trainer: pl.Trainer):
    metrics_file_path = Path(trainer.logger.experiment[-1].log_dir)/'..'/'metrics.csv'
    try:
        df_hist = pd.read_csv(metrics_file_path)
        df_hist['epoch'] = df_hist['epoch'].ffill()
        df_histe = df_hist.set_index('epoch').groupby('epoch').mean()
        return df_histe
    except Exception as e:
        print(e)

def plot_hist(trainer: pl.Trainer):
    """If you used a CSVLogger you can load and plot history here"""
    try:
        df_histe = read_hist(trainer)
        if len(df_histe)>1:
            p = hv.Curve(df_histe, kdims=['epoch'], vdims=['loss/train']).relabel('train')
            p *= hv.Curve(df_histe, kdims=['epoch'], vdims=['loss/val']).relabel('val')
            display(p.opts(ylabel='loss'))
        return df_histe
    except Exception as e:
        print(e)
        pass


# +
# helpers to display our results as a dataframe

def df_bold_min(data):
    '''
    highlight the maximum in a Series or DataFrame
    
    
    Usage:
        `df.style.apply(df_bold_min)`
    '''
    attr = 'font-weight: bold'
    #remove % and cast to float
    data = data.replace('%','', regex=True).astype(float)
    if data.ndim == 1:  # Series from .apply(axis=0) or axis=1
        is_min = data == data.min()
        return [attr if v else '' for v in is_min]
    else:  # from .apply(axis=None)
        is_min = data == data.min().min()
        return pd.DataFrame(np.where(is_min, attr, ''),
                            index=data.index, columns=data.columns)
    
def format_results(results, metric=None, sort=True):
    df_results = pd.concat({k:pd.DataFrame(v) for k,v in results.items()}).T
    if metric:
        df_results = df_results.xs(metric, axis=1, level=1).rename_axis(columns=metric)
        if sort is True:
            df_results['mean(e-e_baseline)'] = (df_results - df_results.T.BaselineMean).mean(1)
            df_results = df_results.sort_values('mean(e-e_baseline)')
    return df_results

def display_results(results, metric='nll', strformat="{:.2f}", sort=True):
    df_results = format_results(results, metric=metric, sort=sort)
    
    # display metric
    display(df_results
            .style.format(strformat)
            .apply(df_bold_min)
           )


# -
# ## Datasets
#
# From easy to hard, these dataset show different challenges, all of them with more than 20k datapoints and with a regression output. See the 00.01 notebook for more details, and the code for more information.
#
# Some such as MetroInterstateTraffic are easier, some are periodic such as BejingPM25, some are conditional on inputs such as GasSensor, and some are noisy and periodic like IMOSCurrentsVel

from seq2seq_time.data.data import IMOSCurrentsVel, AppliancesEnergyPrediction, BejingPM25, GasSensor, MetroInterstateTraffic
datasets = [GasSensor, IMOSCurrentsVel, AppliancesEnergyPrediction, BejingPM25, MetroInterstateTraffic]
datasets



# +
# # View train, test, val splits
# l = hv.Layout()
# for dataset in datasets:
#     d = dataset(datasets_root)
    
#     p = dynspread(
#         datashade(hv.Scatter(d.df_train[d.columns_target[0]]),
#                   cmap='red'))
#     p *= dynspread(
#         datashade(hv.Scatter(d.df_val[d.columns_target[0]]),
#                   cmap='green'))
#     p *= dynspread(
#         datashade(hv.Scatter(d.df_test[d.columns_target[0]]),
#                   cmap='blue'))
#     p = p.opts(title=f"{dataset.__name__}, n={len(d)}, freq={d.df.index.freq.freqstr}")
#     display(p)
# -

# ## Lightning
#
# We will use pytorch lightning to handle all the training scaffolding. We have a common pytorch lightning class that takes in the model and defines training steps and logging.

# +
import pytorch_lightning as pl

class PL_MODEL(pl.LightningModule):
    def __init__(self, model, lr=3e-4, patience=None, weight_decay=0):
        super().__init__()
        self._model = model
        self.lr = lr
        self.patience = patience
        self.weight_decay = weight_decay

    def forward(self, x_past, y_past, x_future, y_future=None):
        """Eval/Predict"""
        y_dist, extra = self._model(x_past, y_past, x_future, y_future)
        return y_dist, extra

    def training_step(self, batch, batch_idx, phase='train'):
        x_past, y_past, x_future, y_future = batch
        y_dist, extra = self.forward(*batch)
        loss = -y_dist.log_prob(y_future).mean()
        self.log_dict({f'loss/{phase}':loss})
        if ('loss' in extra) and (phase=='train'):
            # some models have a special loss
            loss = extra['loss']
            self.log_dict({f'model_loss/{phase}':loss})
        return loss

    def validation_step(self, batch, batch_idx):
        return self.training_step(batch, batch_idx, phase='val')
    
    def configure_optimizers(self):
        optim = torch.optim.AdamW(self.parameters(), lr=self.lr,  weight_decay=self.weight_decay)
        scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(
            optim,
            patience=self.patience,
            verbose=True,
            min_lr=1e-7,
        ) if self.patience else None
        return {'optimizer': optim, 'lr_scheduler': scheduler, 'monitor': 'loss/val'}


# -

from torch.utils.data import DataLoader
from pytorch_lightning.loggers import CSVLogger, WandbLogger, TensorBoardLogger, TestTubeLogger
from pytorch_lightning.callbacks.early_stopping import EarlyStopping
from pytorch_lightning.callbacks import LearningRateMonitor


# ## Models

from seq2seq_time.models.baseline import BaselineLast, BaselineMean
from seq2seq_time.models.lstm_seq2seq import LSTMSeq2Seq
from seq2seq_time.models.lstm import LSTM
from seq2seq_time.models.transformer import Transformer
from seq2seq_time.models.transformer_seq2seq import TransformerSeq2Seq
from seq2seq_time.models.neural_process import RANP
from seq2seq_time.models.transformer_process import TransformerProcess
from seq2seq_time.models.tcn import TCNSeq
from seq2seq_time.models.inceptiontime import InceptionTimeSeq
from seq2seq_time.models.xattention import CrossAttention
# +
import gc

def free_mem():
    gc.collect()
    torch.cuda.empty_cache()
    gc.collect()
# -



# +
# PARAMS: model
## Some datasets are easier, so we will vary the hidden size to predict overfitting
hidden_size={'IMOSCurrentsVel': 8, #?
 'AppliancesEnergyPrediction': 8, # ?
 'BejingPM25': 8, # OK
 'GasSensor': 8, # OK
 'MetroInterstateTraffic': 16 # OK
            }
dropout=0.0
layers=6
nhead=4

models = [
#     lambda xs, ys: BaselineLast(),
    lambda xs, ys, hidden_size: BaselineMean(),
    lambda xs, ys, hidden_size: Transformer(xs,
                ys,
                attention_dropout=dropout,
                nhead=nhead,
                nlayers=layers,
                hidden_size=hidden_size),

    lambda xs, ys, hidden_size:TransformerProcess(xs,
                ys, hidden_size=hidden_size, nhead=nhead,
        latent_dim=hidden_size//2, dropout=dropout,
        nlayers=layers),
    lambda xs, ys, hidden_size:TCNSeq(xs, ys, hidden_size=hidden_size, nlayers=layers, dropout=dropout, kernel_size=2),
    lambda xs, ys, hidden_size: RANP(xs,
        ys, hidden_dim=hidden_size, dropout=dropout, 
         latent_dim=hidden_size//2, n_decoder_layers=layers, n_latent_encoder_layers=layers, n_det_encoder_layers=layers),
    lambda xs, ys, hidden_size: TransformerSeq2Seq(xs,
                       ys,
                       hidden_size=hidden_size,
                       nhead=nhead,
                       nlayers=layers,
                       attention_dropout=dropout
                                     ),
    lambda xs, ys, hidden_size: LSTM(xs,
         ys,
         hidden_size=hidden_size,
         lstm_layers=layers//2,
         lstm_dropout=dropout),
    lambda xs, ys, hidden_size: LSTMSeq2Seq(xs,
                ys,
                hidden_size=hidden_size,
                lstm_layers=layers//2,
                lstm_dropout=dropout),
    lambda xs, ys, hidden_size: CrossAttention(xs,
                ys,
                hidden_size=hidden_size,),
    lambda xs, ys, hidden_size: InceptionTimeSeq(xs,
                ys,
                kernel_size=96,
                layers=layers//2,
                hidden_size=hidden_size,
                bottleneck=hidden_size//4)

]
# -
# Lets summarize all models, and make sure they have a similar number of parameters

# +
# Summarize each models shape and weights
from seq2seq_time.torchsummaryX import summary

# Get a batch
Dataset = datasets[0]
dataset = Dataset(datasets_root)
ds_train, ds_val, ds_test = dataset.to_datasets(window_past=window_past,
                                        window_future=window_future)
dl_val = DataLoader(ds_val, batch_size=batch_size)
batch = next(iter(dl_val))
batch = [x.to(device).float() for x in batch]
x_past, y_past, x_future, y_future = batch
xs = x_past.shape[-1]
ys = y_future.shape[-1]

# summary of each model
sizes=[]
for m_fn in models:
    pt_model = m_fn(xs, ys, 16).eval().to(device)
    model_name = type(pt_model).__name__
    with torch.no_grad():
        df_summary, df_total = summary(pt_model, x_past, y_past, x_future, y_future, print_summary=False)
    sizes.append(df_total.rename(columns={'Totals':model_name}))
df_model_sizes = pd.concat(sizes, 1).T

# Human readable numbers
fmt=pd.io.formats.format.EngFormatter(use_eng_prefix=True)
df_model_sizes_hr = df_model_sizes.apply(lambda x:x.apply(fmt))

df_model_sizes_hr.to_markdown(open(f'../outputs/{timestamp}_models.md', 'w'))
df_model_sizes_hr
# -
# ## Train

from collections import defaultdict
from seq2seq_time.metrics import rmse, smape


max_iters=20000


tensorboard_dir = Path(f"../outputs/{timestamp}").resolve()
print(f'For tensorboard run:\ntensorboard --logdir="{tensorboard_dir}"')

# +
# DEBUG: sanity check

for Dataset in datasets:
    dataset_name = Dataset.__name__
    dataset = Dataset(datasets_root)
    ds_train, ds_val, ds_test = dataset.to_datasets(window_past=window_past,
                                            window_future=window_future)

    # Init data
    x_past, y_past, x_future, y_future = ds_train.get_rows(10)
    xs = x_past.shape[-1]
    ys = y_future.shape[-1]

    # Loaders
    dl_train = DataLoader(ds_train,
                          batch_size=batch_size,
                          shuffle=True,
                          pin_memory=num_workers == 0,
                          num_workers=num_workers)
    dl_val = DataLoader(ds_val,
                         shuffle=True,
                         batch_size=batch_size,
                         num_workers=num_workers)

    for m_fn in models:
        free_mem()
        pt_model = m_fn(xs, ys, hidden_size[dataset_name])
        model_name = type(pt_model).__name__
        print(timestamp, dataset_name, model_name)

        # Wrap in lightning
        model = PL_MODEL(pt_model,
                         lr=3e-4
                        ).to(device)
        trainer = pl.Trainer(
            fast_dev_run=True,
            # GPU
            gpus=1,
            amp_level='O1',
            precision=16,
        )

# +

results = defaultdict(dict)
for Dataset in tqdm(datasets, desc='datasets'):
    dataset_name = Dataset.__name__
    dataset = Dataset(datasets_root)
    ds_train, ds_val, ds_test = dataset.to_datasets(window_past=window_past,
                                            window_future=window_future)

    # Init data
    x_past, y_past, x_future, y_future = ds_train.get_rows(10)
    xs = x_past.shape[-1]
    ys = y_future.shape[-1]

    # Loaders
    dl_train = DataLoader(ds_train,
                          batch_size=batch_size,
                          shuffle=True,
                          pin_memory=num_workers == 0,
                          num_workers=num_workers)
    dl_val = DataLoader(ds_val,
                         shuffle=True,
                         batch_size=batch_size,
                         num_workers=num_workers)

    for m_fn in tqdm(models, desc=f'models ({dataset_name})'):
        try:
            free_mem()
            pt_model = m_fn(xs, ys, hidden_size[dataset_name])
            model_name = type(pt_model).__name__
            print(timestamp, dataset_name, model_name)

            # Wrap in lightning
            patience = 2
            model = PL_MODEL(pt_model,
                             lr=3e-4, patience=patience,
#                              weight_decay=4e-5
                            ).to(device)

            # Trainer            
            save_dir = f"../outputs/{timestamp}/{dataset_name}"
            name  =f'{model_name}'
            trainer = pl.Trainer(
                # Training length
                min_epochs=2,
                max_epochs=100,
                limit_train_batches=max_iters//batch_size,
                limit_val_batches=max_iters//batch_size//5,
                # Misc
                gradient_clip_val=20,
                terminate_on_nan=True,
                # GPU
                gpus=1,
                amp_level='O1',
                precision=16,
                # Logging
                default_root_dir=save_dir,
                logger=[
                    TestTubeLogger(name=name, save_dir=save_dir)
                ],
                # Callbacks
                callbacks=[
                    EarlyStopping(monitor='loss/val', patience=patience * 2),
                    LearningRateMonitor(logging_interval='epoch')
                ],
            )

            # Train
            trainer.fit(model, dl_train, dl_val)

            ds_preds = predict(model.to(device),
                               ds_test,
                               batch_size * 2,
                               device=device,
                               scaler=dataset.output_scaler)

#             display(read_hist(trainer))

            metrics = dict(
                rmse=rmse(ds_preds.y_true, ds_preds.y_pred).item(), 
                smape=smape(ds_preds.y_true, ds_preds.y_pred).item(), 
                nll=ds_preds.nll.mean().item()
                )
            results[dataset_name][model_name] = metrics
            display_results(results, 'nll', sort=False)
            
            pred_path = Path(trainer.logger.experiment[-1].log_dir)/'..'/'ds_preds.nc'
            dset_to_nc(ds_preds, pred_path)
            model.cpu()
        except Exception as e:
            logging.exception('failed to run model')
            
df_results = pd.concat({k:pd.DataFrame(v) for k,v in results.items()})
display(df_results)
# -
# # Leaderboard

print(f'Negative Log-Likelihood (NLL).\nover {window_future} steps')
df_results = pd.concat({k:pd.DataFrame(v) for k,v in results.items()})
display_results(results, 'nll')


def results_html(results, metric='nll', strformat="{:.2f}"):
    df_results = format_results(results, metric=metric)
    f = f'../outputs/{timestamp}_leaderboard.html'
    print('saved to', f)
    df_results.to_html(f, float_format=lambda n:strformat.format(n))

    f = f'../outputs/{timestamp}_leaderboard.md'
    print(f)
    df_results.round(2).to_markdown(open(f, 'w'))
results_html(results, 'nll')

# # Plots
#
# - TODO make legends smaller
# - TODO either many batches, or plot of X steps ahead

# +

# Load saved preds
ds_predss = defaultdict(dict)
for Dataset in datasets:
    dataset_name = Dataset.__name__
    for m_fn in models:
        pt_model = m_fn(xs, ys, hidden_size[dataset_name])
        model_name = type(pt_model).__name__
        save_dir = Path(f"../outputs")/timestamp/dataset_name/model_name
        
        # Get latest checkpoint
        fs = sorted(save_dir.glob("**/ds_preds.nc"))
        if len(fs)>0:
            ds_preds = xr.open_dataset(fs[-1])
            ds_predss[dataset_name][model_name] = ds_preds
# -

data_i = 300

# Plot mean of predictions
n = hv.Layout()
for dataset in ds_predss.keys():
    d = next(iter(ds_predss[dataset].values())).isel(t_source=data_i)
    p = hv_plot_true(d)
    for model in ds_predss[dataset].keys():
        ds_preds = ds_predss[dataset][model]
        d = ds_preds.isel(t_source=data_i)
        p *= hv_plot_pred(d).relabel(label=f"{model}")
    n += p.opts(title=dataset, legend_position='top_left')
n.cols(1).opts(shared_axes=False)



dataset='IMOSCurrentsVel'
data_i=844
n = hv.Layout()
for i, model in enumerate(ds_predss[dataset].keys()):
    ds_preds = ds_predss[dataset][model]
    d = ds_preds.isel(t_source=data_i)
    p = hv_plot_true(d)
    p *= hv_plot_pred(d).relabel('pred')
    p *= hv_plot_std(d)
    n += p.opts(title=f'{dataset} {model}', legend_position='top_left')
n.cols(1)


# +
# 1/0

# +
# Explore predictions with dynamic map

def plot_predictions_ahead(dataset='IMOSCurrentsVel', t_ahead_i=6, start=0, window_steps=1800):
    d = next(iter(ds_predss[dataset].values())).isel(t_ahead=t_ahead_i).isel(t_source=slice(start, start+window_steps))

    p = hv.Scatter({
                'x': d.t_target,
                'y': d.y_true
            }, label='true').opts(color='black', framewise=True)
    for model in results[dataset].keys():
        ds_preds = ds_predss[dataset][model]
        d = ds_preds.isel(t_ahead=t_ahead_i).isel(t_source=slice(start, start+window_steps))
        p *= hv.Curve({'x': d.t_target, 'y':d.y_pred}, label=model).relabel(label=f"{model}")

    p = p.opts(title=f"Dataset: {dataset}, {d.freq}*{t_ahead_i} ahead", height=250, legend_position='top', ylabel=d.targets)
    return p.opts(framewise=True)
    
dmap = hv.DynamicMap(plot_predictions_ahead, kdims=['dataset', 't_ahead_i', 'start', 'window_steps'])
dmap = dmap.redim.values(dataset=list(ds_predss.keys()))
dmap = dmap.redim.range(t_ahead_i=(0, window_future), start=(0, 5000), window_steps=(10, 5000))
dmap = dmap.redim.default(t_ahead_i=10, window_steps=800)
dmap
# -


1/0




# +
# Explore predictions with dynamic map

def plot_predictions_ahead(dataset='IMOSCurrentsVel', t_ahead_i=6, start=0, window_steps=1800):
    ds_preds = ds_predss[dataset]
    l = hv.Layout()
    for model in ds_preds.keys():
        d = ds_preds[model].isel(t_ahead=t_ahead_i).isel(t_source=slice(start, start+window_steps))
        x = d.t_target
        y = d.y_pred
        s = d.y_pred_std

        p = hv.Scatter({
                    'x': d.t_target,
                    'y': d.y_true
                }).opts(color='black')
        p *= hv.Curve({'x': x, 'y':y})
        p *= hv.Spread((x, y, s * 2)).opts(alpha=0.5, line_width=0)
        l += p.opts(title=f"Dataset: {dataset}, model={model}, {d.freq}*{t_ahead_i} ahead", height=250, legend_position='top', ylabel=d.targets)
    return l.cols(1)
    
dmap = hv.DynamicMap(plot_predictions_ahead, kdims=['dataset', 't_ahead_i', 'start', 'window_steps'])
dmap = dmap.redim.values(dataset=list(ds_predss.keys()))
dmap = dmap.redim.range(t_ahead_i=(0, window_future), start=(0, 5000), window_steps=(10, 5000))
dmap = dmap.redim.default(t_ahead_i=10, window_steps=400, dataset='IMOSCurrentsVel')
dmap
# +
# def plot_at_i(time_i, dataset, model):
#     d = ds_predss[dataset][model].isel(t_source=time_i)
#     return hv_plot_prediction(d).relabel(label=f"{model}")

# dmap = hv.DynamicMap(plot_at_i, kdims=['t_source', 'dataset', 'model'])
# t = ds_preds.t_source.values
# models = list(next(iter(ds_predss.values())).keys())
# dmap = dmap.redim.values(
#     t_source=range(len(t)), 
#     dataset=list(ds_predss.keys()),
#     model=models,
# )
# dmap.opts(framewise=True)

# +
# plot_performance(ds_preds, full=True)

# +
# # Explore predictions with dynamic map

# def plot_predictions_ahead(dataset='IMOSCurrentsVel', model='', t_ahead_i=6, start=0, window_steps=1800):
#     d = next(iter(ds_predss[dataset].values())).isel(t_ahead=t_ahead_i).isel(t_source=slice(start, start+window_steps))

#     p = hv.Scatter({
#                 'x': d.t_target,
#                 'y': d.y_true
#             }, label='true').opts(color='black', framewise=True)
    
#     ds_preds = ds_predss[dataset][model]
#     d = ds_preds.isel(t_ahead=t_ahead_i).isel(t_source=slice(start, start+window_steps))
#     x = d.t_target
#     y = d.y_pred
#     s = d.y_pred_std
#     p *= hv.Curve({'x': x, 'y':y}, label=model).relabel(label=f"{model}")
#     p *= hv.Spread((x, y, s * 2),
#                          label='2*std').opts(alpha=0.5, line_width=0)
    
#     p = p.opts(title=f"Dataset: {dataset}, model={model}, {d.freq}*{t_ahead_i} ahead", height=250, legend_position='top', ylabel=d.targets)
#     return p.opts(framewise=True)
    
# dmap = hv.DynamicMap(plot_predictions_ahead, kdims=['dataset', 'model', 't_ahead_i', 'start', 'window_steps'])
# dmap = dmap.redim.values(dataset=list(ds_predss.keys()), model=models)
# dmap = dmap.redim.range(t_ahead_i=(0, window_future), start=(0, 5000), window_steps=(10, 5000))
# dmap = dmap.redim.default(t_ahead_i=10, window_steps=1000)
# dmap
# -








