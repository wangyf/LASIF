#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Functionality for data preprocessing.

:copyright:
    Lion Krischer (krischer@geophysik.uni-muenchen.de), 2013
:license:
    GNU Lesser General Public License, Version 3
    (http://www.gnu.org/copyleft/lesser.html)
"""
import colorama
import multiprocessing
import numpy as np
import obspy
from obspy.xseed import Parser
from Queue import Full as QueueFull
from Queue import Empty as QueueEmpty
from scipy.interpolate import interp1d
from scipy import signal
import sys
import time
import warnings


# File wide lock for reading/writing MiniSEED files.
lock = multiprocessing.Lock()


def zerophase_chebychev_lowpass_filter(trace, freqmax):
    """
    Custom Chebychev type two zerophase lowpass filter useful for decimation
    filtering.

    This filter is stable up to a reduction in frequency with a factor of 10.
    If more reduction is desired, simply decimate in steps.

    Partly based on a filter in ObsPy.

    :param trace: The trace to be filtered.
    :param freqmax: The desired lowpass frequency.

    Will be replaced once ObsPy has a proper decimation filter.
    """
    # rp - maximum ripple of passband, rs - attenuation of stopband
    rp, rs, order = 1, 96, 1e99
    ws = freqmax / (trace.stats.sampling_rate * 0.5)  # stop band frequency
    wp = ws  # pass band frequency

    while True:
        if order <= 12:
            break
        wp *= 0.99
        order, wn = signal.cheb2ord(wp, ws, rp, rs, analog=0)

    b, a = signal.cheby2(order, rs, wn, btype="low", analog=0, output="ba")

    # Apply twice to get rid of the phase distortion.
    trace.data = signal.filtfilt(b, a, trace.data)


def preprocess_file(file_info):
    """
    Function to perform the actual preprocessing for one individual seismogram.

    One goal of this function is to make sure that the data is available at the
    same time steps as the synthetics. The first time sample of the synthetics
    will always be the origin time of the event.

    Furthermore the data has to be converted to m/s.

    :param file_info: A dictionary containing information about the file to
        be processed.

    file_info is dictionary containing the following keys:
        * data_path: Path to the raw data. This has to be read.
        * processed_data_path: The path where the processed file has to be
            saved at.
        * origin_time: The origin time of the event for the file.
        * npts: The number of samples of the synthetic waveform. The data
            should be interpolated to have the same amount of samples.
        * dt: The time increment of the synthetics.
        * station_filename: The filename of the station file for the waveform.
            Can either be a RESP file or a dataless SEED file.
        * highpass: The highpass frequency of the source time function for the
            synthetics.
        * lowpass: The lowpass frequency of the source time function for the
            synthetics.
        * file_number: The number of the file being processed. Useful for
            progress messages.

    Please remember to not touch the lock/mutexes, otherwise it will break with
    current versions of ObsPy. It is probably a good idea to have serial I/O in
    any case, at least with a normal HDD. SSD might be a different issue.
    """
    #==========================================================================
    # Open logfile. Write basic info.
    #==========================================================================
    fid_log = open(file_info["logfile_name"], 'a')

    def log(msg):
        fid_log.write(msg + "\n")
        warnings.warn(msg)
        sys.stdout.flush()

    #==========================================================================
    # Read seismograms and gather basic information.
    #==========================================================================
    log("Processing file %i: %s\n" % (file_info["file_number"],
                                      file_info["data_path"]))

    starttime = file_info["origin_time"]
    endtime = starttime + file_info["dt"] * (file_info["npts"] - 1)
    duration = endtime - starttime

    # The lock is necessary for MiniSEED files. This is a limitation of the
    # current version of ObsPy and will hopefully be resolved soon!
    # Do not remove it!
    lock.acquire()
    st = obspy.read(file_info["data_path"])
    lock.release()

    if len(st) != 1:
        log("File '%s' has %i traces and not 1. Skip all but the first" % (
            file_info["data_path"], len(st)))
    tr = st[0]

    # Trim with a short buffer in an attempt to avoid boundary effects.
    # starttime is the origin time of the event
    # endtime is the origin time plus the length of the synthetics
    tr.trim(starttime - 0.05 * duration, endtime + 0.05 * duration)

    #==========================================================================
    # Some basic checks on the data.
    #==========================================================================
    # Non-zero length
    if not len(tr):
        log("No data contained in time window around the event. "
            "Skipped.\n")
        return True

    # No nans or infinity values allowed.
    if not np.isfinite(tr.data).all():
        log("File '%s' contains NaN. Skipped." % file_info["data_path"])
        return True

    #==========================================================================
    # Step 1: Detrend and taper.
    #==========================================================================
    tr.detrend("demean")
    tr.detrend("linear")
    tr.taper(0.05, type="hann")

    #==========================================================================
    # Step 2: Decimation
    # Decimate with the factor closest to the sampling rate of the synthetics.
    # The data is still oversampled by a large amount so there should be no
    # problems. This has to be done here so that the instrument correction is
    # reasonably fast even for input data with a large sampling rate.
    #==========================================================================
    while True:
        decimation_factor = int(file_info["dt"] / tr.stats.delta)
        # Decimate in steps for large sample rate reductions.
        if decimation_factor > 8:
            decimation_factor = 8
        if decimation_factor > 1:
            new_nyquist = tr.stats.sampling_rate / 2.0 / float(
                decimation_factor)
            zerophase_chebychev_lowpass_filter(tr, new_nyquist)
            tr.decimate(factor=decimation_factor, no_filter=True)
        else:
            break

    #==========================================================================
    # Step 3: Instrument correction
    # Correct seismograms to velocity in m/s.
    #==========================================================================

    station_file = file_info["station_filename"]

    #- check if the station file actually exists ==============================
    if not file_info["station_filename"]:
        log("* Could not find station file for the relevant time "
            "window. Skipped,\n")
        return True

    #- processing for seed files ==============================================
    if "/SEED/" in station_file:
        # XXX: Check if this is m/s. In all cases encountered so far it
        # always is, but SEED is in theory also able to specify corrections
        # to other units...
        paz = Parser(station_file).getPAZ(tr.id, tr.stats.starttime)
        try:
            tr.simulate(paz_remove=paz)
        except ValueError:
            log(("File '%s' could not be corrected with the help of the "
                 "SEED file '%s'. Will be skipped.") %
                (file_info["data_path"], file_info["station_filename"]))
            return True

    #- processing with seed files =============================================
    elif "/RESP/" in station_file:
        try:
            tr.simulate(seedresp={"filename": station_file, "units": "VEL",
                                  "date": tr.stats.starttime})
        except ValueError:
            log(("File '%s' could not be corrected with the help of the "
                 "RESP file '%s'. Will be skipped.") %
                (file_info["data_path"], file_info["station_filename"]))
            return True
    else:
        raise NotImplementedError

    #==========================================================================
    # Step 4: Interpolation
    #==========================================================================
    # Apply one more taper to avoid high frequency contributions from sudden
    # steps at the beginning/end if padded with zeros.
    tr.taper(0.05, type="hann")
    # Make sure that the data array is at least as long as the
    # synthetics array. Also add some buffer sample for the
    # spline interpolation to work in any case.
    buf = file_info["dt"] * 5
    if starttime < (tr.stats.starttime + buf):
        tr.trim(starttime=starttime - buf, pad=True, fill_value=0.0)
    if endtime > (tr.stats.endtime - buf):
        tr.trim(endtime=endtime + buf, pad=True, fill_value=0.0)

    # Actual interpolation. Currently a linear interpolation is used.
    new_time_array = np.linspace(starttime.timestamp, endtime.timestamp,
                                 file_info["npts"])
    old_time_array = np.linspace(tr.stats.starttime.timestamp,
                                 tr.stats.endtime.timestamp, tr.stats.npts)
    tr.data = interp1d(old_time_array, tr.data, kind=1)(new_time_array)
    tr.stats.starttime = starttime
    tr.stats.delta = file_info["dt"]

    #==========================================================================
    # Step 5: Bandpass filtering
    # This has to be exactly the same filter as in the source time function.
    # Should eventually be configurable.
    #==========================================================================
    tr.filter("lowpass", freq=file_info["lowpass"], corners=5, zerophase=False)
    tr.filter("highpass", freq=file_info["highpass"], corners=2,
              zerophase=False)

    #==========================================================================
    # Save processed data and clean up.
    #==========================================================================

    # Convert to single precision for saving.
    tr.data = np.require(tr.data, dtype="float32", requirements="C")
    if hasattr(tr.stats, "mseed"):
        tr.stats.mseed.encoding = "FLOAT32"

    # The lock is necessary for MiniSEED files. This is a limitation of the
    # current version of ObsPy and will hopefully be resolved soon!
    # Do not remove it!
    lock.acquire()
    tr.write(file_info["processed_data_path"], format=tr.stats._format)
    lock.release()

    fid_log.close()

    return True


def worker(receiving_queue, sending_queue):
    """
    Queue for each worker.

    :param receiving_queue: The queue where the jobs are stored.
    :param sending_queue: The quere where the results are stored.
    """
    # Use None as the poison pill to stop the worker.
    for func, args, counter in iter(receiving_queue.get, None):
        args["file_number"] = counter
        result = func(args)
        sending_queue.put(result)


def pool_imap_unordered(function, iterable, processes):
    """
    Custom unordered map implementation. The advantage of this is that it, in
    contrast to the default Pool.map/imap implementations, does not consume the
    whole iterable upfront but only when it needs them. This enables infinitely
    long input queues. This might actually become relevant for LASIF if enough
    data is present.

    :param function: The function to run for each item.
    :param iterable: The iterable yielding items.
    :param processes: The number of processes to launch.
    """
    # Creating the queues for sending and receiving items from the iterable.
    sending_queue = multiprocessing.Queue(processes)
    receiving_queue = multiprocessing.Queue()

    collected_processes = []

    # Start the worker processes.
    for rpt in xrange(processes):
        collected_processes.append(
            multiprocessing.Process(target=worker, args=(sending_queue,
                                    receiving_queue)))

    # Start all processes.
    for proc in collected_processes:
        proc.start()

    # Iterate over the iterable and communicate with the worker process.
    send_len = 0
    recv_len = 0

    try:
        value = iterable.next()
        while True:
            time.sleep(0.1)
            try:
                sending_queue.put((function, value, send_len + 1), True, 0.2)
                send_len += 1
                value = iterable.next()
            except QueueFull:
                while True:
                    try:
                        result = receiving_queue.get(False)
                        recv_len += 1
                        yield result
                    except QueueEmpty:
                        break
    except StopIteration:
        pass

    # Collect all remaining results.
    while recv_len < send_len:
        result = receiving_queue.get(True, 10.0)
        recv_len += 1
        yield result

    # Terminate the worker processes but passing the poison pill.
    for rpt in xrange(processes):
        sending_queue.put(None)

    time.sleep(0.1)
    # Make sure all processes correctly shut down and nothing still lingers
    # around.
    for proc in collected_processes:
        if proc.is_alive():
            proc.terminate()


def launch_processing(data_generator, waiting_time=4.0):
    """
    Launch the parallel processing.

    :param data_generator: A generator yielding file information as required.
    :param waiting_time: The time spent sleeping after the initial message has
        been printed. Useful if the user should be given the chance to cancel
        the processing.
    """
    # Use twice as many processes as cores. The whole operation does a lot of
    # I/O thus more time is available for calculations.
    processes = 2 * multiprocessing.cpu_count()

    print ("%sLaunching preprocessing using %i processes...%s\n"
           "This might take a while. Press Ctrl + C to cancel.\n") % (
        colorama.Fore.GREEN, processes, colorama.Style.RESET_ALL)

    # Give the user some time to read the message.
    time.sleep(waiting_time)

    file_count = 0
    for _ in pool_imap_unordered(preprocess_file, data_generator, processes):
        file_count += 1

    return file_count
