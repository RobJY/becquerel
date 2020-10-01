# -*- coding: utf-8 -*-
import numba as nb
import numpy as np
import warnings
import xarray as xr


class RebinError(Exception):
    """Exception raised by rebin operations."""

    pass


class RebinWarning(UserWarning):
    """Warnings displayed by rebin operations."""

    pass


def _check_ndim(arr, ndim, arr_name='array'):
    """Check the dimensionality of a numpy array

    Check that the array arr has dimension ndim.

    Args:
      arr: numpy array for which dimensionality is checked
      ndim: a scalar number or an iterable
      arr_name: name of array, just for use in the AssertionError message

    Raises:
      AssertionError: if arr does not have dimension ndim
    """
    if (arr.ndim != ndim) and (arr.ndim not in ndim):
        raise RebinError('{}({}) is not {}D'.format(
            arr_name, arr.shape, ndim))


def _check_monotonic_increasing(arr, arr_name='array'):
    """Check that a numpy array is monotonically increasing

    Args:
      arr: numpy array for checking
      arr_name: name of array, just for use in the AssertionError message

    Raises:
      AssertionError: if arr is not monotonically increasing
    """
    # Check that elements along the last axis are increasing or
    # neighboring elements are equal
    tmp = np.diff(arr)
    if not ((tmp > 0) | np.isclose(tmp, 0)).all():
        raise RebinError('{} is not monotonically increasing: {}'.format(
            arr_name, arr))


def _check_partial_overlap(in_edges, out_edges):
    """
    Args:
        in_edges (np.ndarray): an array of the input bin edges (1D or 2D)
            [num_spectra, num_bins_in + 1] or [num_bins_in + 1]
        out_edges (np.ndarray): an array of the output bin edges
            [num_bins_out]

    Raises:
        RebinWarning: for the following cases:

            old:   └┴┴ ...
            new: └┴┴┴┴ ...

            old: ... ┴┴┴┘
            new: ... ┴┴┴┴┴┘

    """
    if (in_edges[..., 0] > out_edges[0]).any():
        warnings.warn(
            'The first input edge is larger than the first output edge, ' +
            'zeros will padded on the left side of the new spectrum',
            RebinWarning)
    if (in_edges[..., -1] < out_edges[-1]).any():
        warnings.warn(
            'The last input edge is smaller than the last output edge, ' +
            'zeros will padded on the right side of the new spectrum',
            RebinWarning)


def _check_any_overlap(in_edges, out_edges):
    """
    Args:
        in_edges (np.ndarray): an array of the input bin edges (1D or 2D)
            [num_spectra, num_bins_in + 1] or [num_bins_in + 1]
        out_edges (np.ndarray): an array of the output bin edges
            [num_bins_out]

    Raises:

        RebinError: for the following cases:

            old:   └┴┴┴┴┘
            new:             └┴┴┴┘

            old:           └┴┴┴┴┘
            new:   └┴┴┴┘
    """
    if (in_edges[..., -1] <= out_edges[0]).any():
        raise RebinError('Input edges are all smaller than output edges')
    if (in_edges[..., 0] >= out_edges[-1]).any():
        raise RebinError('Input edges are all larger than output edges')


def _broadcast(arr, shape):
    """
    broadcast arr out to the first dimension in shape
    specifically for the case 1D -> 2D
    copy req'd: the readonly array doesn't work w/ numba
    """
    if (arr.ndim == 1) and (len(shape) == 2):
        return np.copy(np.broadcast_to(arr, (shape[0], arr.shape[0])))
    else:
        return arr


def _check_shape(arr0, arr1, arr0_name='array0', arr1_name='array1',
                 edges=False):
    arr0_shape = arr0.shape
    arr1_shape = arr1.shape
    if edges:
        if len(arr1_shape) == 1:
            arr1_shape = (arr1_shape[0] - 1,)
        else:
            arr1_shape = (arr1_shape[0], arr1_shape[1] - 1,)
    if arr0_shape != arr1_shape:
        raise RebinError(
            '{}{} does not have a shape compatible with {}{}'.format(
                arr0_name, arr0_shape, arr1_name, arr1_shape))


@nb.vectorize([nb.f8(nb.f8, nb.f8, nb.f8, nb.f8)], nopython=True)
def _linear_offset(slope, cts, low, high):
    """
    Calculate the offset of the linear aproximation of slope when splitting
    counts between bins.

    Args:
      slope:
      cts: counts within the bin
      low: lower bin edge energy
      high: higher bin edge energy

    Returns:
      the offset
    """
    if np.abs(slope) < 1e-6:
        offset = cts / (high - low)
    else:
        offset = (cts - slope / 2. * (high**2 - low**2)) / (high - low)
    return offset


@nb.vectorize([nb.f8(nb.f8, nb.f8, nb.f8)], nopython=True)
def _slope_integral(x, m, b):
    '''
    The indefinite integral of y = mx + b, with an x value substituted in.

    Args:
      x: the x-value
      m: the value of the slope
      b: the y-offset value

    Returns:
      The indefinite integral of y = mx + b, with an x value substituted in.
      (m x^2 / 2 + b x)
    '''
    return m * x**2 / 2 + b * x


@nb.vectorize([nb.f8(nb.f8, nb.f8, nb.f8, nb.f8)], nopython=True)
def _counts(m, b, x_low, x_high):
    '''
    the definite integral of y = mx + b

    Computes the area under a linear approximation of the changing count
    rate in the vincity of relevant bins.  Edges of this integration
    are low and high while offset is provided from _linear_offset and cts
    from the bin being partitioned.

    Args:
      m: the value of slope m
      b: the value of y-offset b
      x_low: the "high" value for x, to be substituted into the integral
      x_high: the "low" value for x, to be substituted into the integral

    Returns:
      the definite integral of y = mx + b
    '''
    return _slope_integral(x_high, m, b) - _slope_integral(x_low, m, b)


def _rebin_interpolation(in_spectra, in_edges, out_edges, slopes):
    """
    Rebins a spectrum using linear interpolation.

    Keeps a running counter of two loop indices: in_idx & out_idx

    N.B. Does not keep Poisson statistics

    Args:
      in_spectra: xr/dask/np array of input spectrum counts in each bin
                  (if xr.DataArray: name of bin dimension should be "bin")
      in_edges: xr/dask/np array of input bin edges
                (if xr.DataArray: name of bin dimension should be "bin_edge")
                (len(in_edges) = (len of dim "bin" of in_spectra) + 1)
      out_edges: xr/dask/np array of output bin edges
      slopes: the slopes of each histogram bin, with the lines drawn between
              each bin edge. (len(slopes) = (len of dim "bin" of in_spectra))

    Returns:
      1D numpy array of rebinned spectrum counts in each bin
    """
    return xr.apply_ufunc(
        _rebin_interpolation_vectorized,
        # note `out_edges[:-1]`: details see _rebin_interpolation_vectorized
        in_spectra, in_edges, out_edges[:-1], slopes,
        input_core_dims=[["bin"], ["bin_edge"], ["bin_edge"], ["bin"]],
        output_core_dims=[["bin"]],
        exclude_dims={"bin", "bin_edge"},  # dimensions allowed to change size
        dask="parallelized",
        # vectorize=True  # only required if the func is not vectorized
    )



@nb.guvectorize([(nb.f8[:], nb.f8[:], nb.f8[:], nb.f8[:], nb.f8[:])],
                "(n),(N),(m),(n)->(m)")
def _rebin_interpolation_vectorized(
        in_spectrum, in_edges, out_edges_no_rightmost, slopes, out_spectrum):
    """
    Rebins a spectrum using linear interpolation.

    Keeps a running counter of two loop indices: in_idx & out_idx

    N.B. Does not keep Poisson statistics

    Args:
      in_spectrum: iterable of input spectrum counts in each bin
      in_edges: iterable of input bin edges (len = len(in_spectrum) + 1)
      out_edges_no_rightmost: iterable of output bin edges
                              (sans the rightmost bin)
      slopes: the slopes of each histogram bin, with the lines drawn between
        each bin edge. (len = len(in_spectrum))
      out_spectrum: for nb.guvectorize; do not actually give this as an arg

    Returns:
      1D numpy array of rebinned spectrum counts in each bin
    """
    # N.B. use [:] to assign values to full array output with guvectorize:
    out_spectrum[:] = np.zeros_like(out_spectrum)  # init output
    # out_edges: might not actually need to create this, since for loops
    # handles under/over-flows.
    # TODO check logic and do whichever is the least computationally expensive
    out_edges = np.concatenate((out_edges_no_rightmost, np.array([np.inf])))
    # in_idx: input bin or left edge
    #         init to the first in_bin which overlaps the 0th out_bin
    in_idx = max(0, np.searchsorted(in_edges, out_edges[0]) - 1)
    # Under-flow handling: Put all counts from in_bins that are completely to
    # the left of the 0th out_bin into the 0th out_bin
    out_spectrum[0] += np.sum(in_spectrum[:in_idx])
    # out_idx: output bin or left edge
    #          init to the first out_bin which overlaps the 0th in_bin
    out_idx = max(0, np.searchsorted(out_edges, in_edges[0]) - 1)
    # loop through input bins (starting from the above in_idx value):
    for in_idx in range(in_idx, len(in_spectrum)):
        # input bin info/data
        in_left_edge = in_edges[in_idx]
        in_right_edge = in_edges[in_idx + 1]
        counts = in_spectrum[in_idx]
        slope = slopes[in_idx]
        offset = _linear_offset(slope, counts, in_left_edge, in_right_edge)
        # loop through output bins that overlap with the current input bin
        for out_idx in range(out_idx, len(out_spectrum)):
            out_left_edge = out_edges[out_idx]
            out_right_edge = out_edges[out_idx + 1]
            if out_left_edge > in_right_edge:
                out_idx -= 1  # rewind back to previous out_bin; not done yet
                break  # break out of out_bins loop, move on to next in_bin
            # Low edge for interpolation
            if out_idx == 0:
                # under-flow handling:
                # all counts in this in_bin goes into the 0th out_bin
                low = in_left_edge  # == min(in_left_edge, out_edges[0])
            else:
                low = max(in_left_edge, out_left_edge)
            # High edge for interpolation
            if out_idx == len(out_spectrum) - 1:
                # over-flow handling:
                # all counts in this in_bin goes into this out_bin
                high = in_right_edge
            else:
                high = min(in_right_edge, out_right_edge)
            # Calc counts for this bin
            out_spectrum[out_idx] += _counts(slope, offset, low, high)
    # nb.guvectorize returns void; specifies output as an "input":
    # return out_spectrum


def _rebin_listmode(in_spectra, in_edges, out_edges):
    """
    Stochastic rebinning method: spectrum-histogram to listmode then back

    All overflow values are put in the leftmost and rightmost bins,
    and note we pass out_edges[:-1] into _rebin_listmode_vectorized.

    TODO Assume piecewise constant (ie steps, flat within each bin)
         distribution for in_spectrum (for now). slopes is unused.

    Args:
      in_spectra: xr/dask/np array of input spectrum counts in each bin
                  in_spectra must have dtype int
                  (if xr.DataArray: name of bin dimension should be "bin")
      in_edges: xr/dask/np array of input bin edges
                (if xr.DataArray: name of bin dimension should be "bin_edge")
                (len(in_edges) = (len of dim "bin" of in_spectra) + 1)
      out_edges: xr/dask/np array of output bin edges

    Returns:
      1D numpy array of rebinned spectrum counts in each bin
    """
    return xr.apply_ufunc(
        _rebin_listmode_vectorized,
        # note `out_edges[:-1]`: see _rebin_listmode_vectorized for details
        in_spectra, in_edges, out_edges[:-1],
        input_core_dims=[["bin"], ["bin_edge"], ["bin_edge"]],
        output_core_dims=[["bin"]],
        exclude_dims={"bin", "bin_edge"},  # dimensions allowed to change size
        dask="parallelized",
        # vectorize=True  # only required if the func is not vectorized
    )


@nb.guvectorize([(nb.i8[:], nb.f8[:], nb.f8[:], nb.i8[:])],
                "(n),(N),(m)->(m)")
def _rebin_listmode_vectorized(
        in_spectrum, in_edges, out_edges_no_rightmost, out_spectrum):
    """
    Stochastic rebinning method: spectrum-histogram to listmode then back

    rightmost edge in the parameter out_edges chopped off, in order for
    nb.guvectorize to work, because it requires the output dimensions
    to be specified by the dimensions of the input parameters
    (i.e. m -> m-1 is not allowed).
    This works since we put all overflow values in the leftmost and rightmost
    bins anyways

    TODO Assume piecewise constant (ie steps, flat within each bin)
         distribution for in_spectrum (for now). slopes is unused.

    Args:
      in_spectrum: iterable of input spectrum counts in each bin
      in_edges: iterable of input bin edges (len = len(in_spectrum) + 1)
      out_edges_no_rightmost: iterable of output bin edges
                              (sans the rightmost bin)
      out_spectrum: for nb.guvectorize; do not actually give this as an arg

    Returns:
      1D numpy array of rebinned spectrum counts in each bin
    """
    # knock out leftmost bin edge too, because we put all overflows into
    # first and last bins anyways
    out_edges = np.concatenate(
        (np.array([-np.inf]), out_edges_no_rightmost[1:], np.array([np.inf])))
    energies = np.zeros(np.sum(in_spectrum))
    energy_idx_start = 0
    # loop through input bins:
    # in_idx = index of in_bin or in_left_edge
    for in_idx in range(len(in_spectrum)):
        bin_counts = in_spectrum[in_idx]
        energy_idx_stop = energy_idx_start + bin_counts
        in_left_edge = in_edges[in_idx]
        in_right_edge = in_edges[in_idx + 1]
        energies[energy_idx_start:energy_idx_stop] = in_left_edge + (
            in_right_edge - in_left_edge) * np.random.rand(bin_counts)
        energy_idx_start = energy_idx_stop
    # bin the energies (drops the energies outside of the out-binning-range)
    # N.B. use [:] to assign values to full array output with guvectorize:
    out_spectrum[:] = np.histogram(energies, bins=out_edges)[0]
    # handling of overflows no longer needed since the first/last out_edges
    # are now -/+ inf
    # # add the under/flow counts back into the out_spectrum
    # out_spectrum[0] += (energies < out_edges[0]).nonzero()[0].size
    # out_spectrum[-1] += (energies > out_edges[-1]).nonzero()[0].size
    # nb.guvectorize returns void; specifies output as an "input":
    # return out_spectrum


def rebin(in_spectra, in_edges, out_edges, method="interpolation",
          slopes=None, zero_pad_warnings=True):
    """
    Spectra rebinning via deterministic or stochastic methods.

    Args:
        in_spectrum (np.ndarray): an array of input spectrum counts (1D or 2D)
            [num_spectra, num_bins_in] or [num_bins_in]
        in_edges (np.ndarray): an array of the input bin edges (1D or 2D)
            [num_spectra, num_bins_in + 1] or [num_bins_in + 1]
        out_edges (np.ndarray): an array of the output bin edges
            [num_bins_out]
        method (str): rebinning method
            "interpolation"
                Deterministic interpolation
            "listmode"
                Stochastic rebinning via conversion to listmode of energies.
                This method will internally convert input spectrum values to
                integers (if necessary) and raise a RebinWarning if the
                conversion results in a decimal precision loss.
        slopes (np.ndarray|None): (optional) an array of input bin slopes for
            quadratic interpolation (1D or 2D)
            (only applies for "interpolation" method)
            [num_spectra, num_bins_in + 1] or [num_bins_in + 1]
        zero_pad_warnings (boolean): warn when edge overlap results in
            appending empty bins

    Raises:
        AssertionError: for bad input arguments

    Returns:
        The rebinned spectrum/a
    """
    method = method.lower()
    # Cast data types and check listmode input
    if method == "listmode":
        if (in_spectra < 0).any():
            raise RebinError('Cannot rebin spectra with negative values with '
                             'listmode method')
        if (in_spectra < 1).all():
            raise RebinError('Cannot rebin spectra with all values less than '
                             'one with listmode method')
        if np.issubdtype(in_spectra.dtype.type, np.floating):
            if np.allclose(in_spectra, np.round(in_spectra)):
                # Don't warn in the case of floats which round to integers
                pass
            else:
                warnings.warn(
                    'Argument in_spectra contains float value(s) which ' +
                    'will have decimal precision loss when converting to ' +
                    'integers for rebin method listmode.',
                    RebinWarning)
            in_spectra = np.asarray(np.round(in_spectra), dtype=np.int64)
    else:
        in_spectra = np.asarray(in_spectra, dtype=np.float64)
    in_edges = np.asarray(in_edges, np.float64)
    out_edges = np.asarray(out_edges, np.float64)
    if slopes is None:
        slopes = np.zeros_like(in_spectra, dtype=np.float64)
    else:
        slopes = np.asarray(slopes, dtype=np.float64)
    # Broadcast 1D -> 2D if necessary
    in_edges = _broadcast(in_edges, in_spectra.shape)
    slopes = _broadcast(slopes, in_spectra.shape)
    # Check dimensions
    _check_ndim(in_spectra, {1, 2}, 'in_spectra')
    _check_ndim(in_edges, {1, 2}, 'in_edges')
    _check_ndim(slopes, {1, 2}, 'slopes')
    _check_ndim(out_edges, 1, 'out_edges')
    # Check shape
    _check_shape(in_spectra, in_edges, 'in_spectra', 'in_edges', edges=True)
    _check_shape(in_spectra, slopes, 'in_spectra', 'slopes')
    # Check for increasing bin structure
    _check_monotonic_increasing(in_edges, 'in_edges')
    _check_monotonic_increasing(out_edges, 'out_edges')
    # Check for bin structure overlap
    _check_any_overlap(in_edges, out_edges)
    if zero_pad_warnings:
        _check_partial_overlap(in_edges, out_edges)
    # Specific calls to different JIT-ed rebinning methods
    if method == "interpolation":
        return _rebin_interpolation(in_spectra, in_edges, out_edges, slopes)
    elif method == "listmode":
        return _rebin_listmode(in_spectra, in_edges, out_edges)
    else:
        raise ValueError("{} is not a valid rebinning method".format(method))
