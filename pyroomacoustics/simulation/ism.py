import math

import numpy as np
from scipy.signal import fftconvolve, hilbert

from .. import libroom
from ..directivities import source_angle_shoebox
from ..parameters import constants
from ..utilities import angle_function


def multi_convolve(*signals):
    max_len = signals[0].shape[-1]
    shape = signals[0].shape[:-1]
    for s in signals[1:]:
        if shape != s.shape[:-1]:
            raise ValueError("All signals to convolve should have same batch shape")

        max_len = max_len + s.shape[1] - 1

    pow2_len = 2 ** math.ceil(np.log2(max_len))

    fd = np.fft.rfft(signals[0], axis=-1, n=pow2_len)
    for s in signals[1:]:
        fd *= np.fft.rfft(s, axis=-1, n=pow2_len)

    conv = np.fft.irfft(fd, axis=-1, n=pow2_len)
    conv = conv[:, :max_len]

    return conv


def apply_air_aborption(
    oct_band_amplitude,
    air_abs_coeffs,
    distance,
):
    air_abs_factor = np.exp(-0.5 * air_abs_coeffs[:, None] * distance)
    return oct_band_amplitude * air_abs_factor


def interpolate_octave_bands(
    octave_bands,
    att_in_octave_bands,
    min_phase=True,
):
    """
    Convert octave band dampings to dft scale, interpolates octave band values to full dft scale values.

    Parameters:
    -------------
    octave_bands: OctaveBands
        The octave bands object that contains the filters
    att_in_octave_band : np.ndarray
        Dampings in octave band Shape : (no_of_octave_band)
    air_abs_band : np.ndarray
        air absorption in octave band Shape : (no_of_octave_band)
    min_phase : Boolean
        decides if the final filter is minimum phase (causal) or (non-causal) linear phase sinc filter

    Returns:
    -------------
    att_in_dft_scale : np.ndarray
        Dampings in octave bands interpolated to full scale frequency domain.

    """
    n_bands = octave_bands.n_bands

    att_in_dft_scale = np.einsum(
        "bi,fb->if", att_in_octave_bands, octave_bands.filters_freq_domain
    )

    if min_phase:
        att_in_dft_scale += (
            1e-07  # To avoid divide by zero error when performing hilbert transform.
        )
        m_p = np.imag(-hilbert(np.log(np.abs(att_in_dft_scale)), axis=-1))
        att_in_dft_scale = np.abs(att_in_dft_scale) * np.exp(1j * m_p)

    ir = np.fft.ifft(att_in_dft_scale, n=octave_bands.n_fft, axis=-1).real

    return ir


def compute_ism_rir(
    src,
    mic,
    mic_dir,
    is_visible,
    fdl,
    c,
    fs,
    octave_bands,
    min_phase=True,
    air_abs_coeffs=None,
):
    fdl2 = fdl // 2

    images = src.images[:, is_visible]
    att = src.damping[:, is_visible]

    dist = np.sqrt(
        np.sum((images - mic[:, None]) ** 2, axis=0)
    )  # Calculate distance between image sources and for each microphone

    # dist shape (n) : n0 of image sources
    time = dist / c  # Calculate time of arrival for each image source

    # we add the delay due to the factional delay filter to
    # the arrival times to avoid problems when propagation
    # is shorter than the delay to to the filter
    # hence: time + fdl2
    delay = fdl2 / fs
    time += delay

    t_max = (
        time.max()
    )  # The image source which takes the most time to arrive to this particular microphone

    # What will be the length of RIR according to t_max
    N = int(math.ceil(t_max * fs + fdl2 + 1))

    oct_band_amplitude = att / dist
    full_band_imp_resp = []

    if air_abs_coeffs is not None:
        oct_band_amplitude = apply_air_aborption(
            oct_band_amplitude, air_abs_coeffs, dist
        )

    if mic_dir is not None:
        angle_function_array = angle_function(images, mic)
        azimuth_m = angle_function_array[0]
        colatitude_m = angle_function_array[1]
        mic_gain = mic_dir.get_response(
            azimuth=azimuth_m,
            colatitude=colatitude_m,
            degrees=False,
        )

        if mic_dir.is_impulse_response:
            full_band_imp_resp.append(mic_gain)
        else:
            oct_band_amplitude *= mic_gain

    if src.directivity is not None:
        azimuth_s, colatitude_s = source_angle_shoebox(
            image_source_loc=images,
            wall_flips=abs(src.orders_xyz[:, is_visible]),
            mic_loc=mic,
        )
        src_gain = src.directivity.get_response(
            azimuth=azimuth_s,
            colatitude=colatitude_s,
            degrees=False,
        )

        if src.directivity.is_impulse_response:
            full_band_imp_resp.append(src_gain)
        else:
            oct_band_amplitude *= src_gain

    # there should be 3 possibile shapes for the gains
    # 1) (n_images,) for freq flat sources
    # 2) (n_images, n_octave_bands) for directivites defined by octave bands
    # 3) (n_images, n_taps) for directivites defined as impulse responses
    # Cases 2-3 are ambiguous, although we will usually have no_of_octave_bands == 7
    # and n_taps > 7
    # Proposed solution: add a type for IR type of impulse responses
    # (MeasuredDirectivity only for now)
    #
    # Then, we can have damping coefficients either
    # 1) (1, n_images) flat
    # 2) (n_octave_bands, n_images) per octave bands
    #
    # flat/octave bands
    # 1) mic_gain * src_gain * damping
    # 2) run rir_builder
    #
    # with impulse response
    # 1) compute flat/oct. bands gains
    #    mic_gain * damping or src_gain * damping or damping
    # 2) compute impulse response part
    #    mic_gain or src_gain or convolve(mic_gain, src_gain)
    # 3) run the dft_scale_rir_calc routine

    n_bands = oct_band_amplitude.shape[0]

    if len(full_band_imp_resp) > 0:
        # Case 3) Full band RIR construction
        sample_frac = time * fs
        time_ip = np.floor(sample_frac).astype(np.int32)
        time_fp = (sample_frac - time_ip).astype(np.float32)

        # create fractional delay filters
        frac_delays = np.zeros((time_fp.shape[0], fdl), dtype=np.float32)
        libroom.fractional_delay(
            frac_delays,
            time_fp,
            constants.get("sinc_lut_granularity"),
            constants.get("num_threads"),
        )
        full_band_imp_resp.append(frac_delays)

        # convolve all the impulse responses
        if n_bands == 1:
            irs = multi_convolve(*full_band_imp_resp)
            irs *= oct_band_amplitude.T

        else:
            ir_att = interpolate_octave_bands(
                octave_bands, oct_band_amplitude, min_phase=min_phase
            )
            full_band_imp_resp.append(ir_att)
            irs = multi_convolve(*full_band_imp_resp)

        # now overlap-add all the short impulse responses
        n_max = int(time_ip.max() + irs.shape[1])
        rir = np.zeros(n_max, dtype=np.float32)
        libroom.delay_sum(
            irs.astype(np.float32), time_ip, rir, constants.get("num_threads")
        )

        if n_bands > 1 and not min_phase:
            # we want to trim the extra samples introduced by the octave
            # band filters
            s = (constants.get("octave_bands_n_fft")) // 2
            rir = rir[s:]

    else:
        # Case 1) or 2)
        # Single- or Octave-bands RIR construction
        n_bands = oct_band_amplitude.shape[0]
        rir = np.zeros(N, dtype=np.float32)  # ir for every band
        for b in range(n_bands):  # Loop through every band
            ir_loc = np.zeros(N, dtype=np.float32)  # ir for every band
            libroom.rir_builder(
                ir_loc,
                time.astype(np.float32),
                oct_band_amplitude[b].astype(np.float32),
                fs,
                fdl,
                constants.get("sinc_lut_granularity"),
                constants.get("num_threads"),
            )

            if n_bands > 1:
                rir += octave_bands.analysis(ir_loc, band=b)
            else:
                rir += ir_loc

    return rir
