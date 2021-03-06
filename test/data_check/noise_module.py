import os
import glob
import math
import datetime
import copy
import time
import matplotlib.pyplot as plt
from numba import jit
import pyasdf
import pandas as pd
import numpy as np
import scipy
from scipy.fftpack import fft,ifft,next_fast_len
from scipy.signal import butter, lfilter, tukey, hilbert, wiener
from scipy.linalg import svd
from scipy.ndimage import map_coordinates
from obspy.signal.filter import bandpass,lowpass
import obspy
from obspy.signal.util import _npts2nfft
from obspy.core.inventory import Inventory, Network, Station, Channel, Site


def stats2inv(stats,resp=None,filexml=None,locs=None):

    # We'll first create all the various objects. These strongly follow the
    # hierarchy of StationXML files.
    inv = Inventory(networks=[],source="japan_from_resp")

    if locs is None:
        net = Network(
            # This is the network code according to the SEED standard.
            code=stats.network,
            # A list of stations. We'll add one later.
            stations=[],
            description="Marine created from SAC and resp files",
            # Start-and end dates are optional.
            start_date=stats.starttime)

        sta = Station(
            # This is the station code according to the SEED standard.
            code=stats.station,
            latitude=stats.sac["stla"],
            longitude=stats.sac["stlo"],
            elevation=stats.sac["stel"],
            creation_date=stats.starttime,
            site=Site(name="First station"))

        cha = Channel(
            # This is the channel code according to the SEED standard.
            code=stats.channel,
            # This is the location code according to the SEED standard.
            location_code=stats.location,
            # Note that these coordinates can differ from the station coordinates.
            latitude=stats.sac["stla"],
            longitude=stats.sac["stlo"],
            elevation=stats.sac["stel"],
            depth=-stats.sac["stel"],
            azimuth=stats.sac["cmpaz"],
            dip=stats.sac["cmpinc"],
            sample_rate=stats.sampling_rate)

    else:
        ista=locs[locs['station']==stats.station].index.values.astype('int64')[0]

        net = Network(
            # This is the network code according to the SEED standard.
            code=locs.iloc[ista]["network"],
            # A list of stations. We'll add one later.
            stations=[],
            description="Marine created from SAC and resp files",
            # Start-and end dates are optional.
            start_date=stats.starttime)

        sta = Station(
            # This is the station code according to the SEED standard.
            code=locs.iloc[ista]["station"],
            latitude=locs.iloc[ista]["latitude"],
            longitude=locs.iloc[ista]["longitude"],
            elevation=locs.iloc[ista]["elevation"],
            creation_date=stats.starttime,
            site=Site(name="First station"))
        cha = Channel(
            # This is the channel code according to the SEED standard.
            code=stats.channel,
            # This is the location code according to the SEED standard.
            location_code=stats.location,
            # Note that these coordinates can differ from the station coordinates.
            latitude=locs.iloc[ista]["latitude"],
            longitude=locs.iloc[ista]["longitude"],
            elevation=locs.iloc[ista]["elevation"],
            depth=-locs.iloc[ista]["elevation"],
            azimuth=0,
            dip=0,
            sample_rate=stats.sampling_rate)

    response = obspy.core.inventory.response.Response()
    if resp is not None:
        print('i dont have the response')
    # By default this accesses the NRL online. Offline copies of the NRL can
    # also be used instead
    # nrl = NRL()
    # The contents of the NRL can be explored interactively in a Python prompt,
    # see API documentation of NRL submodule:
    # http://docs.obspy.org/packages/obspy.clients.nrl.html
    # Here we assume that the end point of data logger and sensor are already
    # known:
    #response = nrl.get_response( # doctest: +SKIP
    #    sensor_keys=['Streckeisen', 'STS-1', '360 seconds'],
    #    datalogger_keys=['REF TEK', 'RT 130 & 130-SMA', '1', '200'])


    # Now tie it all together.
    cha.response = response
    sta.channels.append(cha)
    net.stations.append(sta)
    inv.networks.append(net)

    # And finally write it to a StationXML file. We also force a validation against
    # the StationXML schema to ensure it produces a valid StationXML file.
    #
    # Note that it is also possible to serialize to any of the other inventory
    # output formats ObsPy supports.
    if filexml is not None:
        inv.write(filexml, format="stationxml", validate=True)

    return inv        

def preprocess_raw(st,downsamp_freq,clean_time=True,pre_filt=None,resp=False,respdir=None):
    '''
    pre-process daily stream of data from IRIS server, including:

        - check sample rate is matching (from original process_raw)
        - remove small traces (from original process_raw)
        - remove trend and mean of each trace
        - interpolated to ensure all samples are at interger times of the sampling rate
        - low pass and downsample the data  (from original process_raw)
        - remove instrument response according to the option of resp_option. 
            "inv" -> using inventory information and obspy function of remove_response;
            "spectrum" -> use downloaded response spectrum and interpolate if necessary
            "polezeros" -> use the pole zeros for a crude correction of response
        - trim data to a day-long sequence and interpolate it to ensure starting at 00:00:00.000
    '''

    #----remove the ones with too many segments and gaps------
    if len(st) > 100 or portion_gaps(st) > 0.5:
        print('Too many traces or gaps in Stream: Continue!')
        st=[]
        return st

    #----check sampling rate and trace length----
    st = check_sample(st)
            
    if len(st) == 0:
        print('No traces in Stream: Continue!')
        return st

    delta = st[0].stats.delta
    #-----remove mean and trend for each trace before merge------
    for ii in range(len(st)):
        st[ii].data = np.float32(st[ii].data)
        st[ii].data = scipy.signal.detrend(st[ii].data,type='constant')
        st[ii].data = scipy.signal.detrend(st[ii].data,type='linear')

    st.merge(method=1,fill_value=0)
    sps = st[0].stats.sampling_rate

    if abs(downsamp_freq-sps) > 1E-4:
        #-----low pass filter with corner frequency = 0.9*Nyquist frequency----
        #st[0].data = lowpass(st[0].data,freq=0.4*downsamp_freq,df=sps,corners=4,zerophase=True)
        st[0].data = bandpass(st[0].data,0.01,0.4*downsamp_freq,df=sps,corners=4,zerophase=True)

        #----make downsampling------
        st.interpolate(downsamp_freq,method='weighted_average_slopes')

        delta = st[0].stats.delta
        #-------when starttimes are between sampling points-------
        fric = st[0].stats.starttime.microsecond%(delta*1E6)
        if fric>1E-4:
            st[0].data = segment_interpolate(np.float32(st[0].data),float(fric/delta*1E6))
            #--reset the time to remove the discrepancy---
            st[0].stats.starttime-=(fric*1E-6)

    station = st[0].stats.station

    #-----check whether file folder exists-------
    if resp is not False:
        if resp != 'inv':
            if (respdir is None) or (not os.path.isdir(respdir)):
                raise ValueError('response file folder not found! abort!')

        if resp == 'inv':
            #----check whether inventory is attached----
            if not st[0].stats.response:
                raise ValueError('no response found in the inventory! abort!')
            else:
                print('removing response using inv')
                st.remove_response(output="VEL",pre_filt=pre_filt,water_level=60)

        elif resp == 'spectrum':
            print('remove response using spectrum')
            specfile = glob.glob(os.path.join(respdir,'*'+station+'*'))
            if len(specfile)==0:
                raise ValueError('no response sepctrum found for %s' % station)
            st = resp_spectrum(st[0],specfile[0],downsamp_freq)

        elif resp == 'RESP_files':
            print('using RESP files')
            seedresp = glob.glob(os.path.join(respdir,'RESP.'+station+'*'))
            if len(seedresp)==0:
                raise ValueError('no RESP files found for %s' % station)
            st.simulate(paz_remove=None,pre_filt=pre_filt,seedresp=seedresp)

        elif resp == 'polozeros':
            print('using polos and zeros')
            paz_sts = glob.glob(os.path.join(respdir,'*'+station+'*'))
            if len(paz_sts)==0:
                raise ValueError('no polozeros found for %s' % station)
            st.simulate(paz_remove=paz_sts,pre_filt=pre_filt)

        else:
            raise ValueError('no such option of resp in preprocess_raw! please double check!')

    #-----fill gaps, trim data and interpolate to ensure all starts at 00:00:00.0------
    if clean_time:
        st = clean_daily_segments(st)

    return st

def portion_gaps(stream):
    '''
    get the accumulated gaps (npts) by looking at the accumulated difference between starttime and endtime,
    instead of using the get_gaps function of obspy object of stream. remove the trace if gap length is 
    more than 30% of the trace size. remove the ones with sampling rate not consistent with max(freq) 
    '''
    #-----check the consistency of sampling rate----

    pgaps=0
    npts = (stream[-1].stats.endtime-stream[0].stats.starttime)*stream[0].stats.sampling_rate

    if len(stream)==0:
        return pgaps
    else:

        #----loop through all trace to find gaps----
        for ii in range(len(stream)-1):
            pgaps += (stream[ii+1].stats.starttime-stream[ii].stats.endtime)*stream[ii].stats.sampling_rate

    return pgaps/npts

@jit('float32[:](float32[:],float32)')
def segment_interpolate(sig1,nfric):
    '''
    a sub-function of clean_daily_segments:

    interpolate the data according to fric to ensure all points located on interger times of the
    sampling rate (e.g., starttime = 00:00:00.015, delta = 0.05.)

    input parameters:
    sig1:  float32 -> seismic recordings in a 1D array
    nfric: float32 -> the amount of time difference between the point and the adjacent assumed samples
    '''
    npts = len(sig1)
    sig2 = np.zeros(npts,dtype=np.float32)

    #----instead of shifting, do a interpolation------
    for ii in range(npts):

        #----deal with edges-----
        if ii==0 or ii==npts:
            sig2[ii]=sig1[ii]
        else:
            #------interpolate using a hat function------
            sig2[ii]=(1-nfric)*sig1[ii+1]+nfric*sig1[ii]

    return sig2

def resp_spectrum(source,resp_file,downsamp_freq):
    '''
    remove the instrument response with response spectrum from evalresp.
    the response spectrum is evaluated based on RESP/PZ files and then 
    inverted using obspy function of invert_spectrum. 
    '''
    #--------resp_file is the inverted spectrum response---------
    respz = np.load(resp_file)
    nrespz= respz[1][:]
    spec_freq = max(respz[0])

    #-------on current trace----------
    nfft = _npts2nfft(source.stats.npts)
    sps  = source.stats.sample_rate

    #---------do the interpolation if needed--------
    if spec_freq < 0.5*sps:
        raise ValueError('spectrum file has peak freq smaller than the data, abort!')
    else:
        indx = np.where(respz[0]<=0.5*sps)
        nfreq = np.linspace(0,0.5*sps,nfft)
        nrespz= np.interp(nfreq,respz[0][indx],respz[1][indx])
        
    #----do interpolation if necessary-----
    source_spect = np.fft.rfft(source.data,n=nfft)

    #-----nrespz is inversed (water-leveled) spectrum-----
    source_spect *= nrespz
    source.data = np.fft.irfft(source_spect)[0:source.stats.npts]

    return source

def clean_daily_segments(tr):
    '''
    subfunction to clean the tr recordings. only the traces with at least 0.5-day long
    sequence (respect to 00:00:00.0 of the day) is kept. note that the trace here could
    be of several days recordings, so this function helps to break continuous chunck 
    into a day-long segment from 00:00:00.0 to 24:00:00.0.

    tr: obspy stream object
    '''
    #-----all potential-useful time information-----
    stream_time = tr[0].stats.starttime
    time0 = obspy.UTCDateTime(stream_time.year,stream_time.month,stream_time.day,0,0,0)
    time1 = obspy.UTCDateTime(stream_time.year,stream_time.month,stream_time.day,12,0,0)
    time2 = time1+datetime.timedelta(hours=12)

    #--only keep days with > 0.5-day recordings-
    if stream_time <= time1:
        starttime=time0
    else:
        starttime=time2

    #-----ndays represents how many days from starttime to endtime----
    ndays = round((tr[0].stats.endtime-starttime)/(time2-time0))
    if ndays==0:
        tr=[]
        return tr

    else:
        #-----make a new stream------
        ntr = obspy.Stream()
        ttr = tr[0].copy()
        #----trim a continous segment into day-long sequences----
        for ii in range(ndays):    
            tr[0] = ttr.copy()
            endtime = starttime+datetime.timedelta(days=1)
            tr[0].trim(starttime=starttime,endtime=endtime,pad=True,fill_value=0)

            ntr.append(tr[0])
            starttime = endtime

    return ntr

def make_stationlist_CSV(inv,path):
    '''
    subfunction to output the station list into a CSV file
    inv: inventory information passed from IRIS server
    '''
    #----to hold all variables-----
    netlist = []
    stalist = []
    lonlist = []
    latlist = []
    elvlist = []

    #-----silly inventory structures----
    nnet = len(inv)
    for ii in range(nnet):
        net = inv[ii]
        nsta = len(net)
        for jj in range(nsta):
            sta = net[jj]
            netlist.append(net.code)
            stalist.append(sta.code)
            lonlist.append(sta.longitude)
            latlist.append(sta.latitude)
            elvlist.append(sta.elevation)

    #------------dictionary for a pandas frame------------
    dict = {'network':netlist,'station':stalist,'latitude':latlist,'longitude':lonlist,'elevation':elvlist}
    locs = pd.DataFrame(dict)

    #----------write into a csv file---------------            
    locs.to_csv(os.path.join(path,'locations.txt'),index=False)


def get_event_list(str1,str2,inc_days):
    '''
    return the event list in the formate of 2010_01_01 by taking
    advantage of the datetime modules
    
    str1: string of starting date -> 2010_01_01
    str2: string of ending date -> 2010_10_11
    '''
    event = []
    date1=str1.split('_')
    date2=str2.split('_')
    y1=int(date1[0])
    m1=int(date1[1])
    d1=int(date1[2])
    y2=int(date2[0])
    m2=int(date2[1])
    d2=int(date2[2])
    
    d1=datetime.datetime(y1,m1,d1)
    d2=datetime.datetime(y2,m2,d2)
    dt=datetime.timedelta(days=inc_days)

    while(d1<=d2):
        event.append(d1.strftime('%Y_%m_%d'))
        d1+=dt
    if d1!=d2:
        event.append(d2.strftime('%Y_%m_%d'))
    
    return event

def get_station_pairs(sta):
    '''
    construct station pairs based on the station list
    works same way as the function of itertools
    '''
    pairs=[]
    for ii in range(len(sta)-1):
        for jj in range(ii+1,len(sta)):
            pairs.append((sta[ii],sta[jj]))
    return pairs

@jit('float32(float32,float32,float32,float32)') 
def get_distance(lon1,lat1,lon2,lat2):
    '''
    calculate distance between two points on earth
    
    lon:longitude in degrees
    lat:latitude in degrees
    '''
    R = 6372800  # Earth radius in meters
    pi = 3.1415926536
    
    phi1    = lat1*pi/180
    phi2    = lat2*pi/180
    dphi    = (lat2 - lat1)*pi/180
    dlambda = (lon2 - lon1)*pi/180
    
    a = math.sin(dphi/2)**2+math.cos(phi1)*math.cos(phi2)*math.sin(dlambda/2)**2
    return 2*R*math.atan2(math.sqrt(a), math.sqrt(1 - a))/1000

def get_coda_window(dist,vmin,maxlag,dt,wcoda):
    '''
    calculate the coda wave window for the ccfs based on
    the travel time of the balistic wave and select the 
    index for the time window
    '''
    #--------construct time axis----------
    tt = np.arange(-maxlag/dt, maxlag/dt+1)*dt

    #--get time window--
    tbeg=int(dist/vmin)
    tend=tbeg+wcoda
    if tend>maxlag:
        raise ValueError('time window ends at maxlag, too short!')
    if tbeg>maxlag:
        raise ValueError('time window starts later than maxlag')
    
    #----convert to point index----
    ind1 = np.where(abs(tt)==tbeg)[0]
    ind2 = np.where(abs(tt)==tend)[0]

    if len(ind1)!=2 or len(ind2)!=2:
        raise ValueError('index for time axis is wrong')
    ind = [ind2[0],ind1[0],ind1[1],ind2[1]]

    return ind    

def whiten(data, delta, freqmin, freqmax,Nfft=None):
    """This function takes 1-dimensional *data* timeseries array,
    goes to frequency domain using fft, whitens the amplitude of the spectrum
    in frequency domain between *freqmin* and *freqmax*
    and returns the whitened fft.

    :type data: :class:`numpy.ndarray`
    :param data: Contains the 1D time series to whiten
    :type Nfft: int
    :param Nfft: The number of points to compute the FFT
    :type delta: float
    :param delta: The sampling frequency of the `data`
    :type freqmin: float
    :param freqmin: The lower frequency bound
    :type freqmax: float
    :param freqmax: The upper frequency bound

    :rtype: :class:`numpy.ndarray`
    :returns: The FFT of the input trace, whitened between the frequency bounds
    """

    # Speed up FFT by padding to optimal size for FFTPACK
    if data.ndim == 1:
        axis = 0
    elif data.ndim == 2:
        axis = 1

    if Nfft is None:
        Nfft = next_fast_len(int(data.shape[axis]))

    Napod = 100
    Nfft = int(Nfft)
    freqVec = scipy.fftpack.fftfreq(Nfft, d=delta)[:Nfft // 2]
    J = np.where((freqVec >= freqmin) & (freqVec <= freqmax))[0]
    low = J[0] - Napod
    if low <= 0:
        low = 1

    left = J[0]
    right = J[-1]
    high = J[-1] + Napod
    if high > Nfft/2:
        high = int(Nfft//2)

    FFTRawSign = scipy.fftpack.fft(data, Nfft,axis=axis)
    # Left tapering:
    if axis == 1:
        FFTRawSign[:,0:low] *= 0
        FFTRawSign[:,low:left] = np.cos(
            np.linspace(np.pi / 2., np.pi, left - low)) ** 2 * np.exp(
            1j * np.angle(FFTRawSign[:,low:left]))
        # Pass band:
        FFTRawSign[:,left:right] = np.exp(1j * np.angle(FFTRawSign[:,left:right]))
        # Right tapering:
        FFTRawSign[:,right:high] = np.cos(
            np.linspace(0., np.pi / 2., high - right)) ** 2 * np.exp(
            1j * np.angle(FFTRawSign[:,right:high]))
        FFTRawSign[:,high:Nfft//2] *= 0

        # Hermitian symmetry (because the input is real)
        FFTRawSign[:,-(Nfft//2)+1:] = np.flip(np.conj(FFTRawSign[:,1:(Nfft//2)]),axis=axis)
    else:
        FFTRawSign[0:low] *= 0
        FFTRawSign[low:left] = np.cos(
            np.linspace(np.pi / 2., np.pi, left - low)) ** 2 * np.exp(
            1j * np.angle(FFTRawSign[low:left]))
        # Pass band:
        FFTRawSign[left:right] = np.exp(1j * np.angle(FFTRawSign[left:right]))
        # Right tapering:
        FFTRawSign[right:high] = np.cos(
            np.linspace(0., np.pi / 2., high - right)) ** 2 * np.exp(
            1j * np.angle(FFTRawSign[right:high]))
        FFTRawSign[high:Nfft//2] *= 0

        # Hermitian symmetry (because the input is real)
        FFTRawSign[-(Nfft//2)+1:] = FFTRawSign[1:(Nfft//2)].conjugate()[::-1]
 

    return FFTRawSign


def cross_corr_parameters(source, receiver, start_end_t, source_params,
    receiver_params, locs, maxlag):
    """ 
    Creates parameter dict for cross-correlations and header info to ASDF.

    :type source: `~obspy.core.trace.Stats` object.
    :param source: Stats header from xcorr source station
    :type receiver: `~obspy.core.trace.Stats` object.
    :param receiver: Stats header from xcorr receiver station
    :type start_end_t: `~np.ndarray`
    :param start_end_t: starttime, endtime of cross-correlation (UTCDateTime)
    :type source_params: `~np.ndarray`
    :param source_params: max_mad,max_std,percent non-zero values of source trace
    :type receiver_params: `~np.ndarray`
    :param receiver_params: max_mad,max_std,percent non-zero values of receiver trace
    :type locs: dict
    :param locs: dict with latitude, elevation_in_m, and longitude of all stations
    :type maxlag: int
    :param maxlag: number of lag points in cross-correlation (sample points) 
    :return: Auxiliary data parameter dict
    :rtype: dict

    """

    # source and receiver locations in dict with lat, elevation_in_m, and lon
    source_loc = locs.ix[source['network'] + '.' + source['station']]
    receiver_loc = locs.ix[receiver['network'] + '.' + receiver['station']]

    # # get distance (in km), azimuth and back azimuth
    dist,azi,baz = calc_distance(source_loc,receiver_loc) 

    source_mad,source_std,source_nonzero = source_params[:,0],\
                         source_params[:,1],source_params[:,2]
    receiver_mad,receiver_std,receiver_nonzero = receiver_params[:,0],\
                         receiver_params[:,1],receiver_params[:,2]
    
    starttime = start_end_t[:,0] - obspy.UTCDateTime(1970,1,1)
    starttime = starttime.astype('float')
    endtime = start_end_t[:,1] - obspy.UTCDateTime(1970,1,1)
    endtime = endtime.astype('float')
    source = stats_to_dict(source,'source')
    receiver = stats_to_dict(receiver,'receiver')
    # fill Correlation attribDict 
    parameters = {'source_mad':source_mad,
            'source_std':source_std,
            'source_nonzero':source_nonzero,
            'receiver_mad':receiver_mad,
            'receiver_std':receiver_std,
            'receiver_nonzero':receiver_nonzero,
            'dist':dist,
            'azi':azi,
            'baz':baz,
            'lag':maxlag,
            'starttime':starttime,
            'endtime':endtime}
    parameters.update(source)
    parameters.update(receiver)
    return parameters    

def C3_process(S1_data,S2_data,Nfft,win):
    '''
    performs all C3 processes including 1) cutting the time window for P-N parts;
    2) doing FFT for the two time-seris; 3) performing cross-correlations in freq;
    4) ifft to time domain
    '''
    #-----initialize the spectrum variables----
    ccp1 = np.zeros(Nfft,dtype=np.complex64)
    ccn1 = ccp1
    ccp2 = ccp1
    ccn2 = ccp1
    ccp  = ccp1
    ccn  = ccp1

    #------find the time window for sta1------
    S1_data_N = S1_data[win[0]:win[1]]
    S1_data_N = S1_data_N[::-1]
    S1_data_P = S1_data[win[2]:win[3]]
    S2_data_N = S2_data[win[0]:win[1]]
    S2_data_N = S2_data_N[::-1]
    S2_data_P = S2_data[win[2]:win[3]]

    #---------------do FFT-------------
    ccp1 = scipy.fftpack.fft(S1_data_P, Nfft)
    ccn1 = scipy.fftpack.fft(S1_data_N, Nfft)
    ccp2 = scipy.fftpack.fft(S2_data_P, Nfft)
    ccn2 = scipy.fftpack.fft(S2_data_N, Nfft)

    #------cross correlations--------
    ccp = np.conj(ccp1)*ccp2
    ccn = np.conj(ccn1)*ccn2

    return ccp,ccn
    
def optimized_cc_parameters(dt,maxlag,method,lonS,latS,lonR,latR):
    '''
    provide the parameters for computting CC later
    '''
    dist = get_distance(lonS,latS,lonR,latR)
    parameters = {'dt':dt,
        'dist':dist,
        'lag':maxlag,
        'method':method}
    return parameters

def optimized_correlate1(fft1_smoothed_abs,fft2,maxlag,dt,Nfft,nwin,method="cross-correlation"):
    '''
    Optimized version of the correlation functions: put the smoothed 
    source spectrum amplitude out of the inner for loop. 
    It also takes advantage of the linear relationship of ifft, so that
    stacking in spectrum first to reduce the total number of times for ifft,
    which is the most time consuming steps in the previous correlate function  
    '''

    #------convert all 2D arrays into 1D to speed up--------
    corr = np.zeros(nwin*(Nfft//2),dtype=np.complex64)
    corr = fft1_smoothed_abs.reshape(fft1_smoothed_abs.size,) * fft2.reshape(fft2.size,)

    if method == "coherence":
        temp = moving_ave(np.abs(fft2.reshape(fft2.size,)),10)
        corr /= temp

    corr  = corr.reshape(nwin,Nfft//2)
    ncorr = np.zeros(shape=Nfft,dtype=np.complex64)
    ncorr[:Nfft//2] = np.mean(corr,axis=0)
    ncorr[-(Nfft//2)+1:]=np.flip(np.conj(ncorr[1:(Nfft//2)]),axis=0)
    ncorr[0]=complex(0,0)
    ncorr = np.real(np.fft.ifftshift(scipy.fftpack.ifft(ncorr, Nfft, axis=0)))

    tcorr = np.arange(-Nfft//2 + 1, Nfft//2)*dt
    ind   = np.where(np.abs(tcorr) <= maxlag)[0]
    ncorr = ncorr[ind]
    
    return ncorr

def correlate(fft1,fft2, maxlag,dt, Nfft, method="cross-correlation"):
    """This function takes ndimensional *data* array, computes the cross-correlation in the frequency domain
    and returns the cross-correlation function between [-*maxlag*:*maxlag*].

    :type fft1: :class:`numpy.ndarray`
    :param fft1: This array contains the fft of each timeseries to be cross-correlated.
    :type maxlag: int
    :param maxlag: This number defines the number of samples (N=2*maxlag + 1) of the CCF that will be returned.

    :rtype: :class:`numpy.ndarray`
    :returns: The cross-correlation function between [-maxlag:maxlag]
    """
    # Speed up FFT by padding to optimal size for FFTPACK
    t0=time.time()
    if fft1.ndim == 1:
        axis = 0
        nwin=1
    elif fft1.ndim == 2:
        axis = 1
        nwin= int(fft1.shape[0])

    corr=np.zeros(shape=(nwin,Nfft),dtype=np.complex64)
    corr[:,:Nfft//2]  = np.conj(fft1) * fft2

    if method == 'deconv':
        ind = np.where(np.abs(fft1)>0)
        corr[ind] /= moving_ave(np.abs(fft1[ind]),10)**2
        #corr[ind] /= running_abs_mean(np.abs(fft1[ind]),10) ** 2
    elif method == 'coherence':
        ind = np.where(np.abs(fft1)>0)
        corr[ind] /= running_abs_mean(np.abs(fft1[ind]),5)
        ind = np.where(np.abs(fft2)>0)
        corr[ind] /= running_abs_mean(np.abs(fft2[ind]),5)
    elif method == 'raw':
        ind = 1

    #--------------------problems: [::-1] only flips along axis=0 direction------------------------
    #corr[:,-(Nfft // 2):] = corr[:,:(Nfft // 2)].conjugate()[::-1] # fill in the complex conjugate
    #----------------------------------------------------------------------------------------------
    corr[:,0] = complex(0,0)
    corr[:,-(Nfft//2)+1:]=np.flip(np.conj(corr[:,1:(Nfft//2)]),axis=axis)
    corr = np.real(np.fft.ifftshift(scipy.fftpack.ifft(corr, Nfft, axis=axis)))

    tcorr = np.arange(-Nfft//2 + 1, Nfft//2)*dt
    ind = np.where(np.abs(tcorr) <= maxlag)[0]
    if axis == 1:
        corr = corr[:,ind]
    else:
        corr = corr[ind]
    tcorr=tcorr[ind]

    t1=time.time()
    print('original takes '+str(t1-t0))
    return corr,tcorr


@jit('float32[:](float32[:],int16)')
def moving_ave(A,N):
    '''
    Numba compiled function to do running smooth average.
    N is the the half window length to smooth
    A and B are both 1-D arrays (which runs faster compared to 2-D operations)
    '''
    A = np.r_[A[:N],A,A[-N:]]
    B = np.zeros(A.shape,A.dtype)
    
    tmp=0.
    for pos in range(N,A.size-N):
        # do summing only once
        if pos==N:
            for i in range(-N,N+1):
                tmp+=A[pos+i]
        else:
            tmp=tmp-A[pos-N-1]+A[pos+N]
        B[pos]=tmp/(2*N+1)
        if B[pos]==0:
            B[pos]=1
    return B[N:-N]

def nextpow2(x):
    """
    Returns the next power of 2 of x.

    :type x: int 
    :returns: the next power of 2 of x

    """

    return np.ceil(np.log2(np.abs(x))) 	

def abs_max(arr):
    """
    Returns array divided by its absolute maximum value.

    :type arr:`~numpy.ndarray` 
    :returns: Array divided by its absolute maximum value
    
    """
    
    return (arr.T / np.abs(arr.max(axis=-1))).T


def pws(arr,power=2.,sampling_rate=20.,pws_timegate = 5.):
    """
    Performs phase-weighted stack on array of time series. 

    Follows methods of Schimmel and Paulssen, 1997. 
    If s(t) is time series data (seismogram, or cross-correlation),
    S(t) = s(t) + i*H(s(t)), where H(s(t)) is Hilbert transform of s(t)
    S(t) = s(t) + i*H(s(t)) = A(t)*exp(i*phi(t)), where
    A(t) is envelope of s(t) and phi(t) is phase of s(t)
    Phase-weighted stack, g(t), is then:
    g(t) = 1/N sum j = 1:N s_j(t) * | 1/N sum k = 1:N exp[i * phi_k(t)]|^v
    where N is number of traces used, v is sharpness of phase-weighted stack

    :type arr: numpy.ndarray
    :param arr: N length array of time series data 
    :type power: float
    :param power: exponent for phase stack
    :type sampling_rate: float 
    :param sampling_rate: sampling rate of time series 
    :type pws_timegate: float 
    :param pws_timegate: number of seconds to smooth phase stack
    :Returns: Phase weighted stack of time series data
    :rtype: numpy.ndarray  
    """

    if arr.ndim == 1:
        return arr
    N,M = arr.shape
    analytic = arr + 1j * hilbert(arr,axis=1, N=next_fast_len(M))[:,:M]
    phase = np.angle(analytic)
    phase_stack = np.mean(np.exp(1j*phase),axis=0)/N
    phase_stack = np.abs(phase_stack)**2

    # smoothing 
    timegate_samples = int(pws_timegate * sampling_rate)
    phase_stack = runningMean(phase_stack,timegate_samples)
    weighted = np.multiply(arr,phase_stack)
    return np.mean(weighted,axis=0)/N

def dtw(x,r, g=1.05):
    """ Dynamic Time Warping Algorithm

    Inputs:
    x:     target vector
    r:     vector to be warped

    Outputs:
    D: Distance matrix
    Dist:  unnormalized distance between t and r
    w:     warping path
    
    originally written in MATLAB by Peter Huybers 
    """

    x = norm(x)
    r = norm(r)

    N = len(x)
    M = len(r)

    d = (np.tile(x,(M,1)).T - np.tile(r,(N,1)))**2
    d[0,:] *= 0.25
    d[:,-1] *= 0.25

    D=np.zeros(d.shape)
    D[0,0] = d[0,0]

    for ii in range(1,N):
        D[ii,0] = d[ii,0] + D[ii - 1,0]     

    for ii in range(1,M):
        D[0,ii] = d[0,ii] + D[0,ii-1]

    for ii in range(1,N):
        for jj in range(1,M):
            D[ii,jj] = d[ii,jj] + np.min([g * D[ii - 1, jj], D[ii - 1, jj - 1], g * D[ii, jj - 1]]) 

    dist = D[-1,-1]
    ii,jj,kk = N - 1, M - 1, 1
    w = []
    w.append([ii, jj])
    while (ii + jj) != 0:
        if ii == 0:
            jj -= 1
        elif jj == 0:
            ii -= 1 
        else:
            ind = np.argmin([D[ii - 1, jj], D[ii, jj - 1], D[ii - 1, jj - 1]])
            if ind == 0:
                ii -= 1
            elif ind == 1:
                jj -= 1
            else:
                ii -= 1
                jj -= 1
        kk += 1
        w.append([ii, jj])

    w = np.array(w)
    
    return D,dist,w 


def norm(arr):
    """ Demean and normalize a given input to unit std. """
    arr -= arr.mean(axis=1, keepdims=True)
    return (arr.T / arr.std(axis=-1)).T


def stretch_mat_creation(refcc, str_range=0.01, nstr=1001):
    """ Matrix of stretched instance of a reference trace.

    From the MIIC Development Team (eraldo.pomponi@uni-leipzig.de)
    The reference trace is stretched using a cubic spline interpolation
    algorithm form ``-str_range`` to ``str_range`` (in %) for totally
    ``nstr`` steps.
    The output of this function is a matrix containing the stretched version
    of the reference trace (one each row) (``strrefmat``) and the corresponding
    stretching amount (`strvec```).
    :type refcc: :class:`~numpy.ndarray`
    :param refcc: 1d ndarray. The reference trace that will be stretched
    :type str_range: float
    :param str_range: Amount, in percent, of the desired stretching (one side)
    :type nstr: int
    :param nstr: Number of stretching steps (one side)
    :rtype: :class:`~numpy.ndarray` and float
    :return: **strrefmat**: 2d ndarray of stretched version of the reference trace.
    :rtype: float
    :return: **strvec**: List of float, stretch amount for each row of ``strrefmat``
    """

    n = len(refcc)
    samples_idx = np.arange(-n // 2 + 1, n // 2 + 1)
    strvec = np.linspace(1 - str_range, 1 + str_range, nstr)
    str_timemat = samples_idx / strvec[:,None] + n // 2
    tiled_ref = np.tile(refcc,(nstr,1))
    coord = np.vstack([(np.ones(tiled_ref.shape) * np.arange(tiled_ref.shape[0])[:,None]).flatten(),str_timemat.flatten()])
    strrefmat = map_coordinates(tiled_ref, coord)
    strrefmat = strrefmat.reshape(tiled_ref.shape)
    return strrefmat, strvec


def stretch(data,ref,str_range=0.05,nstr=1001):
    """
    Stretching technique for dt/t. 

    :type data: :class:`~numpy.ndarray`
    :param data: 2d ndarray. Cross-correlation measurements.
    :type ref: :class:`~numpy.ndarray`
    :param ref: 1d ndarray. The reference trace that will be stretched
    :type str_range: float
    :param str_range: Amount, in percent, of the desired stretching (one side)
    :type nstr: int
    :param nstr: Number of stretching steps (one side)
    :rtype: :class:`~numpy.ndarray` 
    :return: **alldeltas**: dt/t for each cross-correlation
    :rtype: :class:`~numpy.ndarray`
    :return: **allcoefs**: Maximum correlation coefficient for each 
         cross-correlation against the reference trace
    :rtype: :class:`~numpy.ndarray`
    :return: **allerrs**: Error for each dt/t measurement

    """
    ref_stretched, deltas = stretch_mat_creation(ref,str_range=str_range,nstr=nstr)
    M,N = data.shape

    alldeltas = np.empty(M,dtype=float)
    allcoefs = np.empty(M,dtype=float)
    allerrs = np.empty(M,dtype=float)
    x = np.arange(nstr)

    for ii in np.arange(M).tolist():
        coeffs = vcorrcoef(ref_stretched,data[ii,:])
        coeffs_shift = coeffs + np.abs(np.min([0,np.min(coeffs)]))
        fw = FWHM(x,coeffs_shift)
        alldeltas[ii] = deltas[np.argmax(coeffs)]
        allcoefs[ii] = np.max(coeffs)
        allerrs[ii] = fw/2
    
    return alldeltas, allcoefs, allerrs

def FWHM(x,y):
    """

    Fast, naive calculation of full-width at half maximum. 

    """
    half_max = np.max(y) / 2.
    left_idx = np.where(y - half_max > 0)[0][0]
    right_idx = np.where(y - half_max > 0)[0][-1]
    return x[right_idx] - x[left_idx]


def pole_zero(inv): 
    """

    Return only pole and zeros of instrument response

    """
    for ii,chan in enumerate(inv[0][0]):
        stages = chan.response.response_stages
        new_stages = []
        for stage in stages:
            if type(stage) == obspy.core.inventory.response.PolesZerosResponseStage:
                new_stages.append(stage)
            elif type(stage) == obspy.core.inventory.response.CoefficientsTypeResponseStage:
                new_stages.append(stage)

        inv[0][0][ii].response.response_stages = new_stages

    return inv

def check_and_phase_shift(trace):
    # print trace
    taper_length = 20.0
    # if trace.stats.npts < 4 * taper_length*trace.stats.sampling_rate:
    # 	trace.data = np.zeros(trace.stats.npts)
    # 	return trace

    dt = np.mod(trace.stats.starttime.datetime.microsecond*1.0e-6,
                trace.stats.delta)
    if (trace.stats.delta - dt) <= np.finfo(float).eps:
        dt = 0
    if dt != 0:
        if dt <= (trace.stats.delta / 2.):
            dt = -dt
        # direction = "left"
        else:
            dt = (trace.stats.delta - dt)
        # direction = "right"
        trace.detrend(type="demean")
        trace.detrend(type="simple")
        taper_1s = taper_length * float(trace.stats.sampling_rate) / trace.stats.npts
        trace.taper(taper_1s)

        n = int(2**nextpow2(len(trace.data)))
        FFTdata = scipy.fftpack.fft(trace.data, n=n)
        fftfreq = scipy.fftpack.fftfreq(n, d=trace.stats.delta)
        FFTdata = FFTdata * np.exp(1j * 2. * np.pi * fftfreq * dt)
        trace.data = np.real(scipy.fftpack.ifft(FFTdata, n=n)[:len(trace.data)])
        trace.stats.starttime += dt
        return trace
    else:
        return trace

def check_sample(stream):
    """
    Returns sampling rate of traces in stream.

    :type stream:`~obspy.core.stream.Stream` object. 
    :param stream: Stream containing one or more day-long trace 
    :return: List of sampling rates in stream

    """
    if len(stream)==0:
        return stream
    else:
        freqs = []	
        for tr in stream:
            freqs.append(tr.stats.sampling_rate)

    freq = max(freqs)
    for tr in stream:
        if tr.stats.sampling_rate != freq:
            stream.remove(tr)

    return stream				

def downsample(stream,freq):
    """ 
    Downsamples stream to specified samplerate.

    Uses Obspy.core.trace.decimate if mod(sampling_rate) == 0. 
    :type stream:`~obspy.core.trace.Stream` or `~obspy.core.trace.Trace` object.
    :type freq: float
    :param freq: Frequency to which waveforms in stream are downsampled
    :return: Downsampled trace or stream object
    :rtype: `~obspy.core.trace.Trace` or `~obspy.core.trace.Trace` object.
    """
    
    # get sampling rate 
    if type(stream) == obspy.core.stream.Stream:
        sampling_rate = stream[0].stats.sampling_rate
    elif type(stream) == obspy.core.trace.Trace:
        sampling_rate = stream.stats.sampling_rate

    if sampling_rate == freq:
        pass
    else:
        stream.interpolate(freq,method="weighted_average_slopes")	

    return stream


def remove_resp(arr,stats,inv):
    """
    Removes instrument response of cross-correlation

    :type arr: numpy.ndarray 
    :type stats: `~obspy.core.trace.Stats` object.
    :type inv: `~obspy.core.inventory.inventory.Inventory`
    :param inv: StationXML file containing response information
    :returns: cross-correlation with response removed
    """	
    
    def arr_to_trace(arr,stats):
        tr = obspy.Trace(arr)
        tr.stats = stats
        tr.stats.npts = len(tr.data)
        return tr

    # prefilter and remove response
    
    st = obspy.Stream()
    if len(arr.shape) == 2:
        for row in arr:
            tr = arr_to_trace(row,stats)
            st += tr
    else:
        tr = arr_to_trace(arr,stats)
        st += tr
    min_freq = 1/tr.stats.npts*tr.stats.sampling_rate
    min_freq = np.max([min_freq,0.005])
    pre_filt = [min_freq,min_freq*1.5, 0.9*tr.stats.sampling_rate, 0.95*tr.stats.sampling_rate]
    st.attach_response(inv)
    st.remove_response(output="VEL",pre_filt=pre_filt) 

    if len(st) > 1: 
        data = []
        for tr in st:
            data.append(tr.data)
        data = np.array(data)
    else:
        data = st[0].data
    return data			

def mad(arr):
    """ 
    Median Absolute Deviation: MAD = median(|Xi- median(X)|)
    :type arr: numpy.ndarray
    :param arr: seismic trace data array 
    :return: Median Absolute Deviation of data
    """
    if not np.ma.is_masked(arr):
        med = np.median(arr)
        data = np.median(np.abs(arr - med))
    else:
        med = np.ma.median(arr)
        data = np.ma.median(np.ma.abs(arr-med))
    return data	
    

def calc_distance(sta1,sta2):
    """ 
    Calcs distance in km, azimuth and back-azimuth between sta1, sta2. 

    Uses obspy.geodetics.base.gps2dist_azimuth for distance calculation. 
    :type sta1: dict
    :param sta1: dict with latitude, elevation_in_m, and longitude of station 1
    :type sta2: dict
    :param sta2: dict with latitude, elevation_in_m, and longitude of station 2
    :return: distance in km, azimuth sta1 -> sta2, and back azimuth sta2 -> sta1
    :rtype: float

    """

    # get coordinates 
    lon1 = sta1['longitude']
    lat1 = sta1['latitude']
    lon2 = sta2['longitude']
    lat2 = sta2['latitude']

    # calculate distance and return 
    dist,azi,baz = obspy.geodetics.base.gps2dist_azimuth(lat1,lon1,lat2,lon2)
    dist /= 1000.
    return dist,azi,baz


def getGaps(stream, min_gap=None, max_gap=None):
    # Create shallow copy of the traces to be able to sort them later on.
    copied_traces = copy.copy(stream.traces)
    stream.sort()
    gap_list = []
    for _i in range(len(stream.traces) - 1):
        # skip traces with different network, station, location or channel
        if stream.traces[_i].id != stream.traces[_i + 1].id:
            continue
        # different sampling rates should always result in a gap or overlap
        if stream.traces[_i].stats.delta == stream.traces[_i + 1].stats.delta:
            flag = True
        else:
            flag = False
        stats = stream.traces[_i].stats
        stime = stats['endtime']
        etime = stream.traces[_i + 1].stats['starttime']
        delta = etime.timestamp - stime.timestamp
        # Check that any overlap is not larger than the trace coverage
        if delta < 0:
            temp = stream.traces[_i + 1].stats['endtime'].timestamp - \
                etime.timestamp
            if (delta * -1) > temp:
                delta = -1 * temp
        # Check gap/overlap criteria
        if min_gap and delta < min_gap:
            continue
        if max_gap and delta > max_gap:
            continue
        # Number of missing samples
        nsamples = int(round(np.abs(delta) * stats['sampling_rate']))
        # skip if is equal to delta (1 / sampling rate)
        if flag and nsamples == 1:
            continue
        elif delta > 0:
            nsamples -= 1
        else:
            nsamples += 1
        gap_list.append([_i, _i+1,
                        stats['network'], stats['station'],
                        stats['location'], stats['channel'],
                        stime, etime, delta, nsamples])
    # Set the original traces to not alter the stream object.
    stream.traces = copied_traces
    return gap_list		

def fft_parameters(dt,cc_len,source,source_times, source_params,locs,component,Nfft,Nt):
    """ 
    Creates parameter dict for cross-correlations and header info to ASDF.

    :type source: `~obspy.core.trace.Stats` object.
    :param source: Stats header from xcorr source station
    :type receiver: `~obspy.core.trace.Stats` object.
    :param receiver: Stats header from xcorr receiver station
    :type start_end_t: `~np.ndarray`
    :param start_end_t: starttime, endtime of cross-correlation (UTCDateTime)
    :type source_params: `~np.ndarray`
    :param source_params: max_mad,max_std,percent non-zero values of source trace
    :type receiver_params: `~np.ndarray`
    :param receiver_params: max_mad,max_std,percent non-zero values of receiver trace
    :type locs: dict
    :param locs: dict with latitude, elevation_in_m, and longitude of all stations
    :type maxlag: int
    :param maxlag: number of lag points in cross-correlation (sample points) 
    :return: Auxiliary data parameter dict
    :rtype: dict

    """
    source_mad,source_std,source_nonzero = source_params[:,0],\
                         source_params[:,1],source_params[:,2]
    lon,lat,el=locs["longitude"],locs["latitude"],locs["elevation"]
    starttime = source_times[:,0]
    endtime = source_times[:,1]
    source = stats_to_dict(source,'source')
    parameters = {'sampling_rate':dt,
             'twin':cc_len,
             'mad':source_mad,
             'std':source_std,
             'nonzero':source_nonzero,
             'longitude':lon,
             'latitude':lat,
             'elevation_in_m':el,
             'component':component,
             'starttime':starttime,
             'endtime':endtime,
             'nfft':Nfft,
             'nseg':Nt}
    parameters.update(source)
    return parameters   

def stats_to_dict(stats,stat_type):
    """
    Converts obspy.core.trace.Stats object to dict
    :type stats: `~obspy.core.trace.Stats` object.
    :type source: str
    :param source: 'source' or 'receiver'
    """
    stat_dict = {'{}_network'.format(stat_type):stats['network'],
                 '{}_station'.format(stat_type):stats['station'],
                 '{}_channel'.format(stat_type):stats['channel'],
                 '{}_delta'.format(stat_type):stats['delta'],
                 '{}_npts'.format(stat_type):stats['npts'],
                 '{}_sampling_rate'.format(stat_type):stats['sampling_rate']}
    return stat_dict 

def stack_parameters(params):
    """
    Creates parameter dict for monthly stack.

    :type params: list (of dicts)
    :param params: List of dicts, created by cross_corr_parameters, for daily cross-correlations

    """

    month = params[0]
    for day in params[1:]:
        month['ccf_windows'].append(day['ccf_windows'])
        month['start_day'].append(day['start_day'])
        month['start_month'].append(day['start_month'])
        month['end_day'].append(day['end_day'])
    month['end_year'].append(day['end_year'])	
    month['end_hour'].append(day['end_hour'])	
    month['end_minute'].append(day['end_minute'])
    month['end_second'].append(day['end_second'])
    month['end_microsecond'].append(day['end_microsecond'])
    return month

def vcorrcoef(X,y):
    """
    Vectorized Cross-correlation coefficient in the time domain

    :type X: `~numpy.ndarray`
    :param X: Matrix containing time series in each row (ndim==2)
    :type X: `~numpy.ndarray`
    :param X: time series array (ndim==1)
    
    :rtype:  `~numpy.ndarray`
    :return: **cc** array of cross-correlation coefficient between rows of X and y
    """
    
    Xm = np.reshape(np.mean(X,axis=1),(X.shape[0],1))
    ym = np.mean(y)
    cc_num = np.sum((X-Xm)*(y-ym),axis=1)
    cc_den = np.sqrt(np.sum((X-Xm)**2,axis=1)*np.sum((y-ym)**2))
    cc = cc_num/cc_den
    return cc

def NCF_denoising(img_to_denoise,Mdate,Ntau,NSV):

	if img_to_denoise.ndim ==2:
		M,N = img_to_denoise.shape
		if NSV > np.min([M,N]):
			NSV = np.min([M,N])
		[U,S,V] = svd(img_to_denoise,full_matrices=False)
		S = scipy.linalg.diagsvd(S,S.shape[0],S.shape[0])
		Xwiener = np.zeros([M,N])
		for kk in range(NSV):
			SV = np.zeros(S.shape)
			SV[kk,kk] = S[kk,kk]
			X = U@SV@V
			Xwiener += wiener(X,[Mdate,Ntau])
			
		denoised_img = wiener(Xwiener,[Mdate,Ntau])
	elif img_to_denoise.ndim ==1:
		M = img_to_denoise.shape[0]
		NSV = np.min([M,NSV])
		denoised_img = wiener(img_to_denoise,Ntau)
		temp = np.trapz(np.abs(np.mean(denoised_img) - img_to_denoise))    
		denoised_img = wiener(img_to_denoise,Ntau,np.mean(temp))

	return denoised_img

if __name__ == "__main__":
    pass

