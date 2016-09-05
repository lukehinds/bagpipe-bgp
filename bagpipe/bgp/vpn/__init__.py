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


from threading import Lock

import re
import logging

from bagpipe.bgp.vpn.ipvpn import IPVPN
from bagpipe.bgp.vpn.ipvpn import VRF
from bagpipe.bgp.vpn.evpn import EVPN
from bagpipe.bgp.vpn.evpn import EVI

import bagpipe.bgp.common.exceptions as exc

from bagpipe.bgp.common import looking_glass as lg
from bagpipe.bgp.common import utils
from bagpipe.bgp.common import log_decorator
from bagpipe.bgp.common.run_command import run_command

from bagpipe.bgp.vpn.label_allocator import LabelAllocator
from bagpipe.bgp.vpn.rd_allocator import RDAllocator

from exabgp.bgp.message.update.attribute.community.extended \
    import RouteTargetASN2Number as RouteTarget


log = logging.getLogger(__name__)


def convert_route_targets(orig_list):
    assert isinstance(orig_list, list)
    list_ = []
    for rt in orig_list:
        if rt == '':
            continue
        try:
            asn, nn = rt.split(':')
            list_.append(RouteTarget(int(asn), int(nn)))
        except Exception:
            raise Exception("Malformed route target: '%s'" % rt)
    return list_


class VPNManager(lg.LookingGlassMixin):

    """
    Creates, and keeps track of, VPN instances (VRFs and EVIs) and passes
    plug/unplug calls to the right VPN instance.
    """

    type2class = {IPVPN: VRF,
                  EVPN: EVI
                  }

    @log_decorator.log
    def __init__(self, bgp_manager, dataplane_drivers):
        '''
        dataplane_drivers is a dict from vpn type to each dataplane driver,
        e.g. { "ipvpn": driverA, "evpn": driverB }
        '''

        self.bgp_manager = bgp_manager

        self.dataplane_drivers = dataplane_drivers

        # Init VPN instance identifiers
        self.instance_id = 1

        # VPN instance dict
        self.vpn_instances = {}

        logging.debug("Creating label allocator")
        self.label_allocator = LabelAllocator()

        logging.debug("Creating route distinguisher allocator")
        self.rd_allocator = RDAllocator(self.bgp_manager.get_local_address())

        # dict containing info how an ipvpn is plugged
        # from an evpn  (keys: ipvpn instances)
        self._evpn_ipvpn_ifs = {}

        self.lock = Lock()

    def _format_ip_address_prefix(self, ip_address):
        if re.match(r'([12]?\d?\d\.){3}[12]?\d?\d\/[123]?\d', ip_address):
            return ip_address
        elif re.match(r'([12]?\d?\d\.){3}[12]?\d?\d', ip_address):
            return ip_address + "/32"
        else:
            raise exc.MalformedIPAddress

    @utils.synchronized
    def get_instance_id(self):
        iid = self.instance_id
        self.instance_id += 1
        return iid

    @log_decorator.log_info
    def _attach_evpn_2_ipvpn(self, localport, ipvpn_instance):
        """ Assuming localport indicates no real interface but only
        an EVPN, this method will create a pair of twin interfaces, one
        to plug in the EVPN, the other to plug in the IPVPN.

        The localport dict will be modified so that the 'linuxif' indicates
        the name of the interface to plug in the IPVPN.

        The EVPN instance will be notified so that it forwards traffic
        destinated to the gateway on the interface toward the IPVPN.
        """
        assert 'evpn' in localport

        if 'id' not in localport['evpn']:
            raise Exception("Missing parameter 'id' :an external EVPN "
                            "instance id must be specified for an EVPN "
                            "attachment")

        try:
            evpn = self.vpn_instances[localport['evpn']['id']]
        except:
            raise Exception("The specified evpn instance does not exist (%s)"
                            % localport['evpn'])

        if evpn.type != EVPN:
            raise Exception("The specified instance to plug is not an evpn"
                            "instance (is %s instead)" % evpn.type)

        if ipvpn_instance in self._evpn_ipvpn_ifs:
            (evpn_if, ipvpn_if, evpn, managed) = \
                self._evpn_ipvpn_ifs[ipvpn_instance]

            if not localport['evpn']['id'] == evpn.external_instance_id:
                raise Exception('Trying to plug into an IPVPN a new E-VPN '
                                'while one is already plugged in')
            else:
                # do nothing
                log.warning('Trying to plug an E-VPN into an IPVPN, but it was'
                            ' already done')
                localport['linuxif'] = ipvpn_if
                return

        #  detect if this evpn is already plugged into an IPVPN
        if evpn.has_gateway_port():
            raise Exception("Trying to plug E-VPN into an IPVPN, but this EVPN"
                            " is already plugged into an IPVPN")

        if 'linuxif' in localport and localport['linuxif']:
            raise Exception("Cannot specify an attachment with both a linuxif "
                            "and an evpn")

        if 'ovs_port_name' in localport['evpn']:
            try:
                assert localport['ovs']['plugged']
                assert(localport['ovs']['port_name'] or
                       localport['ovs']['port_number'])
            except:
                raise Exception("Using ovs_port_name in EVPN/IPVPN attachment"
                                " requires specifying the corresponding OVS"
                                " port, which must also be pre-plugged")

            evpn_if = localport['evpn']['ovs_port_name']

            # we assume in this case that the E-VPN interface is already
            # plugged into the E-VPN bridge
            managed = False
        else:
            evpn_if = "evpn%d-ipvpn%d" % (
                evpn.instance_id, ipvpn_instance.instance_id)
            ipvpn_if = "ipvpn%d-evpn%d" % (
                ipvpn_instance.instance_id, evpn.instance_id)

            # FIXME: do it only if not existing already...
            log.info("Creating veth pair %s %s ", evpn_if, ipvpn_if)

            # delete the interfaces if they exist already
            run_command(log, "ip link delete %s" %
                       evpn_if, acceptable_return_codes=[0, 1])
            run_command(log, "ip link delete %s" %
                       ipvpn_if, acceptable_return_codes=[0, 1])

            run_command(log, "ip link add %s type veth peer name %s mtu 65535" %
                       (evpn_if, ipvpn_if))

            run_command(log, "ip link set %s up" % evpn_if)
            run_command(log, "ip link set %s up" % ipvpn_if)
            managed = True

        localport['linuxif'] = ipvpn_if

        evpn.set_gateway_port(evpn_if, ipvpn_instance)

        self._evpn_ipvpn_ifs[ipvpn_instance] = (
            evpn_if, ipvpn_if, evpn, managed)

    @log_decorator.log_info
    def _detach_evpn_2_ipvpn(self, ipvpn):
        """
        Symmetric to _attach_evpn_2_ipvpn
        """
        (evpn_if, ipvpn_if, evpn_instance,
         managed) = self._evpn_ipvpn_ifs[ipvpn]

        if not ipvpn.has_enpoint(ipvpn_if):
            # TODO: check that this evpn instance is still up and running
            evpn_instance.gateway_port_down(evpn_if)

            # cleanup veth pair
            if managed:
                run_command(log, "ip link delete %s" % evpn_if)

            del self._evpn_ipvpn_ifs[ipvpn]

    def _cleanup_evpn2ipvpn(self, ipvpn):
        (_, ipvpn_if, _, managed) = self._evpn_ipvpn_ifs[ipvpn]

        # cleanup veth pair
        if managed:
            run_command(log, "ovs-vsctl del-port %s" % ipvpn_if)
            run_command(log, "ip link delete %s" % ipvpn_if)

    @log_decorator.log_info
    def _create_vpn_instance(self, external_instance_id, instance_type, import_rts,
                           export_rts, gateway_ip, mask, readvertise,
                           attract_traffic, **kwargs):
        log.info("Create and start new VPN instance %d for external "
                 "instance identifier %s", self.get_instance_id(),
                 external_instance_id)
        try:
            vpn_instance_factory = VPNManager.type2class[instance_type]
        except KeyError:
            log.error("Unsupported instance_type for VPNInstance: %s",
                      instance_type)
            raise Exception("Unsupported instance type: %s" % instance_type)

        try:
            dataplane_driver = self.dataplane_drivers[instance_type]
        except KeyError:
            log.error("No dataplane driver for VPN type %s",
                      instance_type)
            raise Exception("No dataplane driver for VPN type %s" %
                            instance_type)

        vpn_instance = vpn_instance_factory(
            self, dataplane_driver,
            external_instance_id, self.get_instance_id(), import_rts, export_rts,
            gateway_ip, mask, readvertise, attract_traffic, **kwargs)

        # Update VPN instance list
        self.vpn_instances[external_instance_id] = vpn_instance

        vpn_instance.start()

        return vpn_instance

    @log_decorator.log_info
    def plug_vif_to_vpn(self, external_instance_id, instance_type, import_rts,
                     export_rts, mac_address, ip_address, gateway_ip,
                     localport, linuxbr, advertise_subnet, readvertise,
                     attract_traffic, lb_consistent_hash_order):

        # Verify and format IP address with prefix if necessary
        try:
            ip_address_prefix = self._format_ip_address_prefix(ip_address)
        except exc.MalformedIPAddress:
            raise

        # Convert route target string to RouteTarget dictionary
        import_rts = convert_route_targets(import_rts)
        export_rts = convert_route_targets(export_rts)

        if readvertise:
            try:
                readvertise = {k: convert_route_targets(readvertise[k])
                               for k in ['from_rt', 'to_rt']}
            except KeyError as e:
                raise Exception("Wrong 'readvertise' parameters: %s" % e)

        if attract_traffic:
            try:
                attract_traffic['redirect_rts'] = (
                    convert_route_targets(attract_traffic['redirect_rts']))
            except KeyError as e:
                raise Exception("Wrong 'attract_traffic' parameters: %s" % e)

        # retrieve network mask
        mask = int(ip_address_prefix.split('/')[1])

        # Retrieve VPN instance or create new one if does not exist
        try:
            vpn_instance = self.vpn_instances[external_instance_id]
            if vpn_instance.type != instance_type:
                raise Exception("Trying to plug port on an existing instance "
                                "of a different type (existing: %s, asked: %s)"
                                % (vpn_instance.type, instance_type))
        except KeyError:
            if instance_type == EVPN and linuxbr:
                kwargs = {'linuxbr': linuxbr}
            else:
                kwargs = {}

            vpn_instance = self._create_vpn_instance(
                external_instance_id, instance_type, import_rts, export_rts,
                gateway_ip, mask, readvertise, attract_traffic, **kwargs)

        # Check if new route target import/export must be updated
        if not ((set(vpn_instance.import_rts) == set(import_rts)) and
                (set(vpn_instance.export_rts) == set(export_rts))):
            vpn_instance.update_route_targets(import_rts, export_rts)

        if instance_type == IPVPN and 'evpn' in localport:
            # special processing for the case where what we plug into
            # the ipvpn is not an existing interface but an interface
            # to create, connected to an existing evpn instance
            self._attach_evpn_2_ipvpn(localport, vpn_instance)

        # Plug VIF to VPN instance
        vpn_instance.vif_plugged(mac_address, ip_address_prefix, localport,
                               advertise_subnet, lb_consistent_hash_order)

    @log_decorator.log_info
    def unplug_vif_from_vpn(self, external_instance_id, mac_address, ip_address,
                            localport, readvertise):

        # Verify and format IP address with prefix if necessary
        try:
            ip_address_prefix = self._format_ip_address_prefix(ip_address)
        except exc.MalformedIPAddress:
            raise

        # Retrieve VPN instance or raise exception if does not exist
        try:
            vpn_instance = self.vpn_instances[external_instance_id]
        except KeyError:
            log.error("Try to unplug VIF from non existing VPN instance %s",
                      external_instance_id)
            raise exc.VPNNotFound(external_instance_id)

        # Unplug VIF from VPN instance
        vpn_instance.vif_unplugged(mac_address, ip_address_prefix, readvertise)

        if vpn_instance.type == IPVPN and 'evpn' in localport:
            self._detach_evpn_2_ipvpn(vpn_instance)

        if vpn_instance.stop_if_empty():
            del self.vpn_instances[external_instance_id]

    @log_decorator.log_info
    def redirect_traffic_to_vpn(self, redirected_id,
                                redirected_type, redirect_rt):
        external_instance_id = "redirect-to-%s-%s" % (redirected_type,
                                                      redirect_rt.replace(":",
                                                                          "_"))

        log.info("Retrieve VPN instance %s for traffic redirection to route "
                 "target %s", external_instance_id, redirect_rt)

        # Retrieve a redirect VPN instance or create a new one if none exists
        # yet
        try:
            redirect_instance = self.vpn_instances[external_instance_id]
            if redirect_instance.type != redirected_type:
                raise Exception("Trying to redirect traffic to an existing "
                                "instance of a different type (existing: %s, "
                                "asked: %s)"
                                % (redirect_instance.type, redirected_type))
        except KeyError:
            # Convert route target string to RouteTarget dictionary
            import_rts = convert_route_targets([redirect_rt])

            redirect_instance = self._create_vpn_instance(
                external_instance_id, redirected_type, import_rts, [],
                "127.0.0.1", "24", None, None)

        redirect_instance.register_redirected_instance(redirected_id)

        return redirect_instance

    @log_decorator.log_info
    def stop_redirect_to_vpn(self, redirected_id,
                            redirected_type, redirect_rt):
        #TODO: factor out in a function:
        external_instance_id = "redirect-to-%s-%s" % (redirected_type,
                                                      redirect_rt.replace(":",
                                                                          "_"))
        #TODO: factor out in a function:
        try:
            redirect_instance = self.vpn_instances[external_instance_id]
        except KeyError:
            log.error("Try to stop traffic redirection to non existing VPN "
                      "instance %s", external_instance_id)
            raise exc.VPNNotFound(external_instance_id)

        redirect_instance.unregister_redirected_instance(redirected_id)

        if redirect_instance.stop_if_no_redirected_instance():
            del self.vpn_instances[external_instance_id]

    @log_decorator.log_info
    def stop(self):
        for vpn_instance in self.vpn_instances.itervalues():
            vpn_instance.stop()
            # Cleanup veth pair
            if (vpn_instance.type == IPVPN and
                    self._evpn_ipvpn_ifs.get(vpn_instance)):
                self._cleanup_evpn2ipvpn(vpn_instance)
        for vpn_instance in self.vpn_instances.itervalues():
            vpn_instance.join()

    # Looking Glass hooks ####

    def get_lg_map(self):
        class DataplaneLGHook(lg.LookingGlassMixin):

            def __init__(self, vpn_manager):
                self.vpn_manager = vpn_manager

            def get_lg_map(self):
                return {
                    "drivers": (lg.COLLECTION, (
                        self.vpn_manager.get_lg_dataplanes_list,
                        self.vpn_manager.get_lg_dataplane_from_path_item)),
                    "ids": (lg.DELEGATE, self.vpn_manager.label_allocator)
                }
        dataplane_hook = DataplaneLGHook(self)
        return {
            "instances": (lg.COLLECTION, (self.get_lg_vpn_list,
                                          self.get_lg_vpn_from_path_item)),
            "dataplane": (lg.DELEGATE, dataplane_hook)
        }

    def get_lg_vpn_list(self):
        return [{"id": id,
                 "name": instance.name}
                for (id, instance) in self.vpn_instances.iteritems()]

    def get_lg_vpn_from_path_item(self, path_item):
        return self.vpn_instances[path_item]

    def get_vpn_instances_count(self):
        return len(self.vpn_instances)

    def get_lg_dataplanes_list(self):
        return [{"id": i} for i in self.dataplane_drivers.iterkeys()]

    def get_lg_dataplane_from_path_item(self, path_item):
        return self.dataplane_drivers[path_item]
