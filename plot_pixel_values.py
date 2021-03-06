#!/usr/bin/env python
import os
from time import sleep
import argparse

import logging
import pyyaks.logger
from scipy.ndimage.filters import median_filter
import numpy as np
import matplotlib.pyplot as plt
from astropy.table import Table
from sherpa import ui
from Chandra.Time import DateTime
from Ska.Matplotlib import plot_cxctime, cxctime2plotdate


def get_opt():
    parser = argparse.ArgumentParser(description='Plot pixel values in real time')
    parser.add_argument('--pix-filename',
                        default='pixel_values.dat',
                        help='Input pixel values filename')
    parser.add_argument('--logfile',
                        help='Output log filename')
    parser.add_argument('--start',
                        help='Start time (default=2000:001)')
    parser.add_argument('--plot-fit-curves',
                        action='store_true',
                        help="Plot dark current vs t_ccd curves and fits")
    parser.add_argument('--n-brightest',
                        default=64,
                        type=int,
                        help='Plot the N brightest (must be n**2)')

    args = parser.parse_args()
    return args

opt = get_opt()
if opt.logfile is None:
    root, ext = os.path.splitext(opt.pix_filename)
    opt.logfile = "{}.log".format(root)

pix_log = pyyaks.logger.get_logger(name='pix_log',
                                   filename=opt.logfile,
                                   filemode='a',
                                   level=logging.INFO)


T_CCD_REF = -19 # Reference temperature for dark current values in degC
def dark_scale_model(pars, t_ccd):
    """
    dark_t_ref : dark current of a pixel at the reference temperature
    scale : dark current model scale factor
    returns : dark_t_ref scaled to the observed temperatures t_ccd
    """
    scale, dark_t_ref = pars
    scaled_dark_t_ref = dark_t_ref * np.exp(np.log(scale) / 4.0 * (T_CCD_REF - t_ccd))
    return scaled_dark_t_ref


def fit_pix_values(t_ccd, esec, id=1):
    logger = logging.getLogger("sherpa")
    logger.setLevel(logging.WARN)
    data_id = id
    ui.clean()
    ui.set_method('simplex')
    ui.load_user_model(dark_scale_model, 'model')
    ui.add_user_pars('model', ['scale', 'dark_t_ref'])
    ui.set_model(data_id, 'model')
    ui.load_arrays(data_id,
                   np.array(t_ccd),
                   np.array(esec),
                   )
    ui.set_staterror(data_id, 30 * np.ones(len(t_ccd)))
    model.scale.val = 0.588
    model.scale.min = 0.3
    model.scale.max = 1.0
    model.dark_t_ref.val = 500
    ui.freeze(model.scale)
    # If more than 5 degrees in the temperature range,
    # thaw and fit for model.scale.  Else just use/return
    # the fit of dark_t_ref
    if np.max(t_ccd) - np.min(t_ccd) > 2:
        # Fit first for dark_t_ref
        ui.fit(data_id)
        ui.thaw(model.scale)
    ui.fit(data_id)
    return ui.get_fit_results(), ui.get_model(data_id)


def print_info_block(fits, last_dat):
    pix_log.info("*************************************************")
    pix_log.info("Time = {}".format(DateTime(last_dat['time']).date))
    pix_log.info("CCD temperature = {}".format(last_dat['TEMPCD']))
    pix_log.info("Slot = {}\n".format(last_dat['SLOT']))
    pix_log.info("Fit values:\n")
    mini_table = []
    other_t_ccd = [0, 10, 15, 20, 25]
    for pix_id in sorted(fits):
        fitinfo = fits[pix_id]
        if fitinfo is None:
            continue
        m = fitinfo['modpars']
        dc = dark_scale_model((m.scale.val, m.dark_t_ref.val), last_dat['TEMPCD'])
        ref_dc = dark_scale_model((m.scale.val, m.dark_t_ref.val), -19)
        scale_factor = ref_dc / dc
        rec_esec = last_dat[pix_id] * GAIN / last_dat['INTEG']
        minus_19_esec =  rec_esec * scale_factor
        new_rec = [pix_id, rec_esec, minus_19_esec, m.scale.val, dc / ref_dc]
        for t_ccd in other_t_ccd:
            dc_temp = dark_scale_model((m.scale.val, m.dark_t_ref.val), t_ccd)
            new_rec.append(dc_temp / ref_dc)
        mini_table.append(new_rec)
    if not len(mini_table):
        return
    colnames = ['PixId', 'e-/sec', 'e-/sec(-19)', 'Scale', 'r({:.1f})'.format(last_dat['TEMPCD'])]
    for t_ccd in other_t_ccd:
        colnames.append("r({})".format(t_ccd))
    mini_table = Table(rows=mini_table,
                       names=colnames)
    mini_table['e-/sec'].format = '.2f'
    mini_table['e-/sec(-19)'].format = '.2f'
    mini_table['Scale'].format = '.4f'
    for col in mini_table.colnames:
        if col.startswith('r('):
            mini_table[col].format = '.2f'
    mini_table.sort('e-/sec')
    pix_log.info(mini_table)
    pix_log.info("*************************************************")


plt.close(1)
plt.close("fitplots")
plt.close("ccdplot")
plt.ion()

N = np.int(np.sqrt(opt.n_brightest))

fig, axes = plt.subplots(N, N, sharex=True, sharey=True,
                         num=1, figsize=(8, 8))
fig.subplots_adjust(left=0, bottom=0, right=1, top=1, wspace=0, hspace=0)
axes[0][0].set_xticklabels([])
axes[0][0].set_yticklabels([])

if opt.plot_fit_curves:
    fitfig, fitaxes = plt.subplots(N, N, sharex=True, sharey=True,
                                   num="fitplots", figsize=(8, 8))
    fitfig.subplots_adjust(left=0, bottom=0, right=1, top=1, wspace=0, hspace=0)
    fitaxes[0][0].set_xticklabels([])
    fitaxes[0][0].set_yticklabels([])


colnames = ['r{}_c{}'.format(r, c)
            for r in range(8)
            for c in range(8)]

start = DateTime(opt.start or '2000:001')
GAIN = 5.0

while True:
    dat = Table.read(opt.pix_filename, format='ascii.basic', guess=False,
                     fast_reader=True)
    # Filter:
    #  first record (usually bad in splat output)
    #  unknown/unset temperature data (TEMPCD = -99)
    #  records with very large INTEG time (bad decom?)
    #  records before set start time
    dat = dat[1:]
    dat = dat[dat['TEMPCD'] != -99]
    dat = dat[dat['TEMPCD'] > -17]
    dat = dat[dat['INTEG'] < 2.0]
    dat = dat[dat['r5_c2'] != 1759.75]
    dat = dat[dat['r6_c5'] != 791.0]
    dat = dat[dat['r2_c7'] != 584.0]
    dat = dat[dat['r7_c5'] != 470.0]
    dat = dat[dat['r2_c0'] != 410.0]
    dat = dat[dat['r0_c6'] != 235.0]
    dat = dat[dat['r0_c7'] != 217.0]
    dat = dat[dat['time'] > start.secs]
    dat['dt'] = dat['time'] - dat['time'][0]
    integ = dat['INTEG']

    ccdfig = plt.figure("ccdplot")
    ccdax = plt.gca()
    if ccdax.lines:
        ccdline = ccdax.lines[0]
        ccdline.set_data(cxctime2plotdate(dat['time']), dat['TEMPCD'])
        ccdax.relim()
        ccdax.autoscale_view()
    else:
        plot_cxctime(dat['time'], dat['TEMPCD'], 'b.', ax=ccdax)

    #for colname in colnames:
    #    dat[colname] = median_filter(dat[colname], 5)
    maxes = [np.max(median_filter(dat[colname], 5)) for colname in colnames]

    i_brightest = np.argsort(maxes)[-opt.n_brightest:]
    cols = []
    for i, colname in enumerate(colnames):
        if i in i_brightest:
            cols.append(dat[colname])

    i_col = 0
    fits = {}
    for r in range(N):
        for c in range(N):
            ax = axes[r][c]
            x = dat['dt']
            y = cols[i_col] * GAIN / integ
            i_col += 1
            if ax.lines:
                l0 = ax.lines[0]
                l0.set_data(x, y)
                ax.relim()
                ax.autoscale_view()
            else:
                ax.plot(x, y)
            ax.texts = []
            ax.annotate("{}".format(y.name),
                        xy=(0.5, 0.5), xycoords="axes fraction",
                        ha='center', va='center',
                        color='grey')
            t_ccd = dat['TEMPCD']
            try:
                fit, modpars = fit_pix_values(t_ccd,
                                              y,
                                              id=i_col)
                fits[y.name] = {'fit': fit,
                                'modpars': modpars}
                fitmod = ui.get_model_plot(i_col)
            except Exception as exception:
                pix_log.warn('Sherpa fit failed on {}'.format(y.name))
                pix_log.warn(exception)
                pix_log.warn('Continuing')
                fits[y.name] = None
                continue
            if len(ax.lines) > 1:
                l1 = ax.lines[1]
                l1.set_data(x, fitmod.y)
                ax.relim()
                ax.autoscale_view()
            else:
                ax.plot(x, fitmod.y, color='red')
            if opt.plot_fit_curves:
                fitax = fitaxes[r][c]
                fitax.clear()
                fitax.plot(t_ccd, y, '.',
                           markersize=2.5, color='red')
                mp = ui.get_model_plot(i_col)
                fitax.plot(mp.x, mp.y, 'k')
                fitax.texts = []
                fitax.annotate("{}".format(y.name),
                               xy=(0.5, 0.5), xycoords="axes fraction",
                               ha='center', va='center',
                               color='grey')

    print_info_block(fits, dat[-1])
    plt.draw()
    fig.suptitle("Slot {}".format(dat[-1]['SLOT']))
    fig.canvas.draw()
    fig.canvas.flush_events()
    if opt.plot_fit_curves:
        fitfig.suptitle("Slot {}".format(dat[-1]['SLOT']))
        fitfig.canvas.draw()
        fitfig.canvas.flush_events()

    sleep(5)
