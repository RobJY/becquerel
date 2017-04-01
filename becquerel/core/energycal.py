from abc import ABCMeta, abstractmethod, abstractproperty
import numpy as np


class EnergyCalError(Exception):
    """Base class for errors in energycal.py"""

    pass


class BadInput(EnergyCalError):
    """Error related to energy cal input"""

    pass


class EnergyCalBase(object):
    """Abstract base class for energy calibration."""

    __metaclass__ = ABCMeta

    def __init__(self):
        """Create an empty calibration instance.

        Normally you should use from_points or from_coeffs classmethods.
        """

        self._calpoints = dict()
        self._coeffs = dict()
        # initialize fit constraints?

    @classmethod
    def from_points(cls, chlist=None, kevlist=None, pairlist=None):
        """Construct EnergyCal from calibration points."""

        if pairlist and (chlist or kevlist):
            raise BadInput('Redundant calibration inputs')
        if (chlist and not kevlist) or (kevlist and not chlist):
            raise BadInput('Require both chlist and kevlist')
        if not chlist and not kevlist and not pairlist:
            raise BadInput('Calibration points are required')
        if chlist and kevlist:
            if len(chlist) != len(kevlist):
                raise BadInput('Channels and energies must be same length')
            pairlist = zip(chlist, kevlist)

        cal = cls()

        for ch, kev in pairlist:
            # TODO check integrity of pairlist
            cal.add_calpoint(ch, kev)

        return cal

    @classmethod
    def from_coeffs(cls, coeffs):
        """Construct EnergyCal from equation coefficients dict."""

        cal = cls()

        for coeff, val in coeffs:
            cal._set_coeff(coeff, val)

    def add_calpoint(self, ch, kev):
        """Add a calibration point (ch, kev) pair. May be new or existing."""

        self._calpoints[float(kev)] = float(ch)

    def new_calpoint(self, ch, kev):
        """Add a new calibration point. Error if energy matches existing point.
        """

        if kev in self._calpoints:
            raise EnergyCalError('Calibration energy already exists')
        self.add_calpoint(ch, kev)

    def update_calpoint(self, ch, kev):
        """Update a calibration point. Error if it doesn't exist."""

        if kev in self._calpoints:
            self.add_calpoint(ch, kev)
        else:
            raise EnergyCalError('Calibration energy for updating not found')

    def rm_calpoint(self, kev):
        """Remove a calibration point."""

        if kev in self._calpoints:
            del self._calpoints[kev]
        # TODO erroring version?

    @property
    def channels(self):
        return np.array(self._calpoints.values)

    @property
    def energies(self):
        return np.array(self._calpoints.keys)

    @property
    def calpoints(self):
        return zip(self.channels, self.energies)

    @property
    def coeffs(self):
        return self._coeffs

    def ch2kev(self, ch):
        """Convert channel(s) to energy value(s)."""

        ch_array = np.array(ch)
        kev_array = self._ch2kev(ch_array)
        if np.isscalar(ch):
            return float(kev_array)
        else:
            return kev_array

    @abstractmethod
    def _ch2kev(self, ch_array):
        """Convert np.array of channel(s) to energies. Internal method."""

        pass

    @abstractmethod
    def kev2ch(self, kev):
        """Convert energy value(s) to channel(s)."""

        # if this is not possible, raise a NotImplementedError ?
        pass

    @abstractproperty
    def valid_coeffs(self):
        """A list of valid coefficients for the calibration curve."""

        pass

    def _set_coeff(self, name, val):
        """Set a coefficient for the calibration curve."""

        if name in self.valid_coeffs:
            self._coeffs[name] = val
        else:
            raise EnergyCalError('Invalid coefficient name: {}'.format(name))

    def update_fit(self):
        """Compute the calibration curve from the current points."""

        num_coeffs = len(self._coeffs)
        # TODO: free coefficients, not all coefficients
        num_points = len(self._calpoints)

        if num_points == 0:
            raise EnergyCalError('No calibration points; cannot calibrate')
        elif num_points < num_coeffs:
            raise EnergyCalError('Not enough calibration points to fit curve')
        else:
            self._perform_fit()

    @abstractmethod
    def _perform_fit(self):
        """Do the actual curve fitting."""

        pass


class LinearEnergyCal(EnergyCalBase):
    """
    kev = b*ch + c
    """

    @classmethod
    def from_coeffs(self, coeffs):
        # allow other names for linear coefficients
        new_coeffs = {}
        if 'p0' in coeffs and 'p1' in coeffs:
            new_coeffs['b'] = coeffs['p1']
            new_coeffs['c'] = coeffs['p0']
        elif 'slope' in coeffs and 'offset' in coeffs:
            new_coeffs['b'] = coeffs['slope']
            new_coeffs['c'] = coeffs['offset']
        elif 'm' in coeffs and 'b' in coeffs:
            new_coeffs['b'] = coeffs['m']
            new_coeffs['c'] = coeffs['b']
        cal = super().from_coeffs(new_coeffs)
        return cal

    def valid_coeffs(self):
        return ('b', 'c')

    @property
    def slope(self):
        return self._coeffs['b']

    @property
    def offset(self):
        return self._coeffs['c']

    def _ch2kev(self, ch_array):
        return self.slope * ch_array + self.offset

    def kev2ch(self, kev):
        return (kev - self.offset) / self.slope

    def _perform_fit(self):
        b, c = np.polyfit(self.channels, self.energies, 1)
        self._set_coeff('b', b)
        self._set_coeff('c', c)
