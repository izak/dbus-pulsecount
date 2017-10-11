#!/usr/bin/python -u

import sys, os
import signal
from threading import Thread
from select import select, epoll, EPOLLPRI
from functools import partial
from collections import namedtuple
from argparse import ArgumentParser
import traceback
sys.path.insert(1, os.path.join(os.path.dirname(__file__), 'ext', 'velib_python'))

from dbus.mainloop.glib import DBusGMainLoop
import dbus
import gobject
from vedbus import VeDbusService
from settingsdevice import SettingsDevice

VERSION = '0.1'
MAXCOUNT = 2**31-1
SAVEINTERVAL = 60000

INPUT_FUNCTION_COUNTER = 1
INPUT_FUNCTION_INPUT = 2

Product = namedtuple('Point', ['id', 'name', 'product'])

FUNCTIONS = {
    INPUT_FUNCTION_COUNTER: Product('pulsemeter', 'Pulse meter', 0xA163),
    INPUT_FUNCTION_INPUT: Product('digitalinput', 'Digital input', 0xA164),
}

# Only append at the end
INPUTTYPES = [
    'Door alarm',
    'Bilge alarm',
    'Burglar alarm',
    'Smoke alarm',
    'Fire alarm',
    'CO2 alarm'
]
MAXTYPE = len(INPUTTYPES)

class SystemBus(dbus.bus.BusConnection):
	def __new__(cls):
		return dbus.bus.BusConnection.__new__(cls, dbus.bus.BusConnection.TYPE_SYSTEM)

class SessionBus(dbus.bus.BusConnection):
	def __new__(cls):
		return dbus.bus.BusConnection.__new__(cls, dbus.bus.BusConnection.TYPE_SESSION)

class BasePulseCounter(object):
    pass

class DebugPulseCounter(BasePulseCounter):
    def __init__(self):
        self.gpiomap = {}

    def register(self, path, gpio):
        self.gpiomap[gpio] = None

    def unregister(self, gpio):
        del self.gpiomap[gpio]

    def registered(self, gpio):
        return gpio in self.gpiomap

    def __call__(self):
        from itertools import cycle
        from time import sleep
        for level in cycle([0, 1]):
            gpios = self.gpiomap.keys()
            for gpio in gpios:
                yield gpio, level
                sleep(0.25/len(self.gpiomap))

class EpollPulseCounter(BasePulseCounter):
    def __init__(self):
        self.fdmap = {}
        self.gpiomap = {}
        self.ob = epoll()

    def register(self, path, gpio):
        path = os.path.realpath(path)

        # Set up gpio for rising edge interrupts
        with open(os.path.join(os.path.dirname(path), 'edge'), 'ab') as fp:
            fp.write('both')

        fp = open(path, 'rb')
        fp.read() # flush it in case it's high at startup
        self.fdmap[fp.fileno()] = gpio
        self.gpiomap[gpio] = fp
        self.ob.register(fp, EPOLLPRI)

    def unregister(self, gpio):
        fp = self.gpiomap[gpio]
        self.ob.unregister(fp)
        del self.gpiomap[gpio]
        del self.fdmap[fp.fileno()]
        fp.close()

    def registered(self, gpio):
        return gpio in self.gpiomap

    def __call__(self):
        while True: 
            # We have a timeout of 1 second on the poll, because poll() only
            # looks at files in the epoll object at the time poll() was called.
            # The timeout means we let other files (added via calls to
            # register/unregister) into the loop at least that often.
            for fd, evt in self.ob.poll(1):
                os.lseek(fd, 0, os.SEEK_SET)
                v = os.read(fd, 1)
                yield self.fdmap[fd], int(v)

def dbusconnection():
    # dbus already ensures singleton-behaviour
    return SessionBus() if 'DBUS_SESSION_BUS_ADDRESS' in os.environ else SystemBus()

def main():
    parser = ArgumentParser(description=sys.argv[0])
    parser.add_argument('--servicebase',
        help='Base service name on dbus, default is com.victronenergy',
        default='com.victronenergy')
    parser.add_argument('--debug',
        help='Enable debug counter, this ignores the real gpios and simulates input',
        default=False, action="store_true")
    parser.add_argument('inputs', nargs='+', help='Path to digital input')
    args = parser.parse_args()

    if args.debug:
        PulseCounter = DebugPulseCounter
    else:
        PulseCounter = EpollPulseCounter

    DBusGMainLoop(set_as_default=True)

    # Keep track of enabled services
    services = {}
    inputs = dict(enumerate(args.inputs, 1))
    pulses = PulseCounter() # callable that iterates over pulses
    settings = {}

    def get_volume_text(gpio, path, value):
        return str(value) + ' cubic meter'

    def register_gpio(path, gpio, f):
        print "Registering GPIO {} for function {}".format(gpio, f)

        function = FUNCTIONS[f]
        services[gpio] = dbusservice = VeDbusService(
            "{}.{}.input{:02d}".format(args.servicebase, function.id, gpio), bus=dbusconnection())

        # Add objects required by ve-api
        dbusservice.add_path('/Management/ProcessName', __file__)
        dbusservice.add_path('/Management/ProcessVersion', VERSION)
        dbusservice.add_path('/Management/Connection', path)
        dbusservice.add_path('/DeviceInstance', gpio)
        dbusservice.add_path('/ProductId', function.product) # None set, FIXME?
        dbusservice.add_path('/ProductName', function.name)
        dbusservice.add_path('/Connected', 1)

        dbusservice.add_path('/Count', value=0)
        dbusservice['/Count'] = settings[gpio]['count']
        if f == INPUT_FUNCTION_COUNTER:
            dbusservice.add_path('/Aggregate', value=0,
                gettextcallback=partial(get_volume_text, gpio))
            dbusservice['/Aggregate'] = settings[gpio]['count'] * settings[gpio]['rate']
        elif f == INPUT_FUNCTION_INPUT:
            dbusservice.add_path('/State', value=settings[gpio]['invert'])
            dbusservice.add_path('/Type',
                value=INPUTTYPES[min(settings[gpio]['inputtype'], MAXTYPE-1)])
        pulses.register(path, gpio)

    def unregister_gpio(gpio):
        print "unRegistering GPIO {}".format(gpio)
        pulses.unregister(gpio)
        services[gpio].__del__()
        del services[gpio]

    # Interface to settings
    def handle_setting_change(inp, setting, old, new):
        if setting == 'function':
            if new:
                # Input enabled. If already enabled, unregister the old one first.
                if pulses.registered(inp):
                    unregister_gpio(inp)
                register_gpio(inputs[inp], inp, int(new))
            elif old:
                # Input disabled
                unregister_gpio(inp)
        elif setting == 'inputtype':
            if new != old:
                services[inp]['/Type'] = INPUTTYPES[min(int(new), MAXTYPE-1)]

    for inp, pth in inputs.items():
        supported_settings = {
            'function': ['/Settings/DigitalInput/{}/Function'.format(inp), 0, 0, 2],
            'inputtype': ['/Settings/DigitalInput/{}/Type'.format(inp), 0, 0, MAXTYPE],
            'rate': ['/Settings/DigitalInput/{}/Multiplier'.format(inp), 0.001, 0, 1.0],
            'count': ['/Settings/DigitalInput/{}/Count'.format(inp), 0, 0, MAXCOUNT, 1],
            'invert': ['/Settings/DigitalInput/{}/Inverted'.format(inp), 0, 0, 1]
        }
        settings[inp] = sd = SettingsDevice(dbusconnection(), supported_settings, partial(handle_setting_change, inp), timeout=10)
        if sd['function'] > 0:
            register_gpio(pth, inp, int(sd['function']))

    def poll(mainloop):
        from time import time
        #stamps = { inp: [0] * 5 for inp in gpios }
        idx = 0

        try:
            for inp, level in pulses():
                # epoll object only resyncs once a second. We may receive
                # a pulse for something that's been deregistered.
                try:
                    dbusservice = services[inp]
                except KeyError:
                    continue
                function = settings[inp]['function']
                invert = bool(settings[inp]['invert'])
                level ^= invert

                # Only increment Count on rising edge.
                if level:
                    countpath = '/Count'
                    v = (dbusservice[countpath]+1) % MAXCOUNT
                    dbusservice[countpath] = v
                    if function == INPUT_FUNCTION_COUNTER:
                        dbusservice['/Aggregate'] = v * settings[inp]['rate']

                if function == INPUT_FUNCTION_INPUT:
                    dbusservice['/State'] = bool(level)*1
        except:
            traceback.print_exc()
            mainloop.quit()

    # Need to run the gpio polling in separate thread. Pass in the mainloop so
    # the thread can kill us if there is an exception.
    gobject.threads_init()
    mainloop = gobject.MainLoop()

    poller = Thread(target=lambda: poll(mainloop))
    poller.daemon = True
    poller.start()

    # Periodically save the counter
    def save_counters():
        for inp in inputs:
            if settings[inp]['function'] > 0:
                settings[inp]['count'] = services[inp]['/Count']
        return True
    gobject.timeout_add(SAVEINTERVAL, save_counters)

    # Save counter on shutdown
    signal.signal(signal.SIGTERM, lambda *args: sys.exit(0))

    try:
        mainloop.run()
    except KeyboardInterrupt:
        pass
    finally:
        save_counters()

if __name__ == "__main__":
    main()
