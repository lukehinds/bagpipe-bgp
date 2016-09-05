# vim: tabstop=4 shiftwidth=4 softtabstop=4
# encoding: utf-8

# Copyright 2014 Orange
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


import sys
import traceback
import logging

import re

from collections import defaultdict


def import_class(import_str):
    """Returns a class from a string including module and class"""
    mod_str, _, class_str = import_str.rpartition('.')
    logging.debug("Trying to import module %s", mod_str)
    try:
        __import__(mod_str)
        logging.debug("Trying to get class %s", class_str)
        return getattr(sys.modules[mod_str], class_str)
    except AttributeError:
        raise ImportError("No '%s' class in %s" % (class_str, mod_str))
    except ValueError as e:
        logging.debug("Exception occurred during import: %s", e)
        raise ImportError('Class %s cannot be found (%s)' %
                          (class_str,
                           traceback.format_exception(*sys.exc_info())))
    except Exception as e:
        logging.debug(
            "Exception while trying to import class %s: %s", import_str, e)
        raise


def import_object(import_str, *args, **kwargs):
    """Import a class and return an instance of it."""
    return import_class(import_str)(*args, **kwargs)


def synchronized(method):

    def synchronized_method(self, *arg, **kws):
        with self.lock:
            return method(self, *arg, **kws)

    return synchronized_method


def get_boolean(string):
    '''
    return True is string represents boolean true ("true","yes","on","1"),
    False if not
    '''
    if isinstance(string, bool):
        return string
    assert isinstance(string, str)
    return string.lower() in ["true", "yes", "on", "1"]


def plural(x):
    if len(x) > 1:
        return "s"
    else:
        return ""


def invert_dict_of_sets(d):
    '''
    return inverted dict of sets from original dict containing sets of
    non-unique hashable items
    '''
    new_d = defaultdict(set)
    for k in d:
        for v in d[k]:
            new_d[v].add(k)
    return new_d


camel2underscore_regex = re.compile('(?!^)([A-Z]+)')

def dict_camelcase_to_underscore(dictionary):
    ''' copy dict, with translation of keys from FooBar to foo_bar'''
    return {camel2underscore_regex.sub(r'_\1', key).lower(): value
            for (key, value) in dictionary.iteritems()
            }
