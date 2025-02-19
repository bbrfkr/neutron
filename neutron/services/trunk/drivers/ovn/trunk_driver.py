#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

from neutron_lib.api.definitions import portbindings
from neutron_lib.callbacks import events
from neutron_lib.callbacks import registry
from neutron_lib.callbacks import resources
from neutron_lib import constants as n_const
from neutron_lib import context as n_context
from neutron_lib.db import api as db_api
from neutron_lib import exceptions as n_exc
from neutron_lib.services.trunk import constants as trunk_consts
from oslo_config import cfg
from oslo_log import log

from neutron.common.ovn import constants as ovn_const
from neutron.common import utils as n_utils
from neutron.db import db_base_plugin_common
from neutron.db import ovn_revision_numbers_db as db_rev
from neutron.objects import ports as port_obj
from neutron.services.trunk.drivers import base as trunk_base
from neutron.services.trunk import exceptions as trunk_exc


SUPPORTED_INTERFACES = (
    portbindings.VIF_TYPE_OVS,
    portbindings.VIF_TYPE_VHOST_USER,
)

SUPPORTED_SEGMENTATION_TYPES = (
    trunk_consts.SEGMENTATION_TYPE_VLAN,
)

LOG = log.getLogger(__name__)


class OVNTrunkHandler(object):
    def __init__(self, plugin_driver):
        self.plugin_driver = plugin_driver

    def _set_sub_ports(self, parent_port, subports):
        txn = self.plugin_driver.nb_ovn.transaction
        context = n_context.get_admin_context()
        db_parent_port = port_obj.Port.get_object(context, id=parent_port)
        parent_port_status = db_parent_port.status
        try:
            parent_port_bindings = [pb for pb in db_parent_port.bindings
                                    if pb.status == n_const.ACTIVE][-1]
        except IndexError:
            parent_port_bindings = None
        for subport in subports:
            with db_api.CONTEXT_WRITER.using(context), (
                    txn(check_error=True)) as ovn_txn:
                port = self._set_binding_profile(context, subport, parent_port,
                                                 parent_port_status,
                                                 parent_port_bindings, ovn_txn)
            db_rev.bump_revision(context, port, ovn_const.TYPE_PORTS)

    def _unset_sub_ports(self, subports):
        txn = self.plugin_driver.nb_ovn.transaction
        context = n_context.get_admin_context()
        for subport in subports:
            with db_api.CONTEXT_WRITER.using(context), (
                    txn(check_error=True)) as ovn_txn:
                port = self._unset_binding_profile(context, subport, ovn_txn)
            db_rev.bump_revision(context, port, ovn_const.TYPE_PORTS)

    @db_base_plugin_common.convert_result_to_dict
    def _set_binding_profile(self, context, subport, parent_port,
                             parent_port_status,
                             parent_port_bindings, ovn_txn):
        LOG.debug("Setting parent %s for subport %s",
                  parent_port, subport.port_id)
        db_port = port_obj.Port.get_object(context, id=subport.port_id)
        if not db_port:
            LOG.debug("Port not found while trying to set "
                      "binding_profile: %s",
                      subport.port_id)
            return
        check_rev_cmd = self.plugin_driver.nb_ovn.check_revision_number(
            db_port.id, db_port, ovn_const.TYPE_PORTS)
        ovn_txn.add(check_rev_cmd)
        parent_binding_host = ''
        if parent_port_bindings and parent_port_bindings.host:
            migrating_to = parent_port_bindings.profile.get(
                ovn_const.MIGRATING_ATTR)
            parent_binding_host = migrating_to or parent_port_bindings.host
        try:
            # NOTE(flaviof): We expect binding's host to be set. Otherwise,
            # sub-port will not transition from DOWN to ACTIVE.
            db_port.device_owner = trunk_consts.TRUNK_SUBPORT_OWNER
            # NOTE(ksambor):  When sub-port was created and event was process
            # without binding profile this port will end forever in DOWN
            # status so we need to switch it here to the parent port status
            db_port.status = parent_port_status
            for binding in db_port.bindings:
                binding.profile['parent_name'] = parent_port
                binding.profile['tag'] = subport.segmentation_id
                # host + port_id is primary key
                port_obj.PortBinding.update_object(
                    context,
                    {'profile': binding.profile,
                     'host': parent_binding_host,
                     'vif_type': portbindings.VIF_TYPE_OVS},
                    port_id=subport.port_id,
                    host=binding.host)
            db_port.update()
        except n_exc.ObjectNotFound:
            LOG.debug("Port not found while trying to set "
                      "binding_profile: %s", subport.port_id)
            return
        ext_ids = {ovn_const.OVN_DEVICE_OWNER_EXT_ID_KEY: db_port.device_owner}
        ovn_txn.add(self.plugin_driver.nb_ovn.set_lswitch_port(
            lport_name=subport.port_id,
            parent_name=parent_port,
            tag=subport.segmentation_id,
            external_ids_update=ext_ids,
        ))
        LOG.debug("Done setting parent %s for subport %s",
                  parent_port, subport.port_id)
        return db_port

    @db_base_plugin_common.convert_result_to_dict
    def _unset_binding_profile(self, context, subport, ovn_txn):
        LOG.debug("Unsetting parent for subport %s", subport.port_id)
        db_port = port_obj.Port.get_object(context, id=subport.port_id)
        if not db_port:
            LOG.debug("Port not found while trying to unset "
                      "binding_profile: %s",
                      subport.port_id)
            return
        check_rev_cmd = self.plugin_driver.nb_ovn.check_revision_number(
            db_port.id, db_port, ovn_const.TYPE_PORTS)
        ovn_txn.add(check_rev_cmd)
        try:
            db_port.device_owner = ''
            for binding in db_port.bindings:
                binding.profile.pop('tag', None)
                binding.profile.pop('parent_name', None)
                # host + port_id is primary key
                port_obj.PortBinding.update_object(
                    context,
                    {'profile': binding.profile,
                     'vif_type': portbindings.VIF_TYPE_UNBOUND},
                    port_id=subport.port_id,
                    host=binding.host)
                port_obj.PortBindingLevel.delete_objects(
                    context, port_id=subport.port_id, host=binding.host)
            db_port.update()
        except n_exc.ObjectNotFound:
            LOG.debug("Port not found while trying to unset "
                      "binding_profile: %s", subport.port_id)
            return
        ext_ids = {ovn_const.OVN_DEVICE_OWNER_EXT_ID_KEY: db_port.device_owner}
        ovn_txn.add(self.plugin_driver.nb_ovn.set_lswitch_port(
            lport_name=subport.port_id,
            parent_name=[],
            up=False,
            tag=[],
            external_ids_update=ext_ids,
        ))
        LOG.debug("Done unsetting parent for subport %s", subport.port_id)
        return db_port

    @staticmethod
    def _is_port_bound(port):
        return n_utils.is_port_bound(port, log_message=False)

    def trunk_updated(self, trunk):
        # Check if parent port is handled by OVN.
        if not self.plugin_driver.nb_ovn.lookup('Logical_Switch_Port',
                                                trunk.port_id, default=None):
            return
        if trunk.sub_ports:
            self._set_sub_ports(trunk.port_id, trunk.sub_ports)

    def trunk_created(self, trunk):
        # Check if parent port is handled by OVN.
        if not self.plugin_driver.nb_ovn.lookup('Logical_Switch_Port',
                                                trunk.port_id, default=None):
            return
        if trunk.sub_ports:
            self._set_sub_ports(trunk.port_id, trunk.sub_ports)
        trunk.update(status=trunk_consts.TRUNK_ACTIVE_STATUS)

    def trunk_deleted(self, trunk):
        if trunk.sub_ports:
            self._unset_sub_ports(trunk.sub_ports)

    def subports_added(self, trunk, subports):
        # Check if parent port is handled by OVN.
        if not self.plugin_driver.nb_ovn.lookup('Logical_Switch_Port',
                                                trunk.port_id, default=None):
            return
        if subports:
            self._set_sub_ports(trunk.port_id, subports)
        trunk.update(status=trunk_consts.TRUNK_ACTIVE_STATUS)

    def subports_deleted(self, trunk, subports):
        # Check if parent port is handled by OVN.
        if not self.plugin_driver.nb_ovn.lookup('Logical_Switch_Port',
                                                trunk.port_id, default=None):
            return
        if subports:
            self._unset_sub_ports(subports)
        trunk.update(status=trunk_consts.TRUNK_ACTIVE_STATUS)

    def trunk_event(self, resource, event, trunk_plugin, payload):
        if event == events.AFTER_CREATE:
            self.trunk_created(payload.states[0])
        elif event == events.AFTER_UPDATE:
            self.trunk_updated(payload.states[0])
        elif event == events.AFTER_DELETE:
            self.trunk_deleted(payload.states[0])
        elif event == events.PRECOMMIT_CREATE:
            trunk = payload.desired_state
            parent_port = trunk.db_obj.port
            if self._is_port_bound(parent_port):
                raise trunk_exc.ParentPortInUse(port_id=parent_port.id)
        elif event == events.PRECOMMIT_DELETE:
            trunk = payload.states[0]
            parent_port = payload.states[1]
            if self._is_port_bound(parent_port):
                raise trunk_exc.TrunkInUse(trunk_id=trunk.id)

    def subport_event(self, resource, event, trunk_plugin, payload):
        if event == events.AFTER_CREATE:
            self.subports_added(payload.states[0],
                                payload.metadata['subports'])
        elif event == events.AFTER_DELETE:
            self.subports_deleted(payload.states[0],
                                  payload.metadata['subports'])


class OVNTrunkDriver(trunk_base.DriverBase):
    @property
    def is_loaded(self):
        try:
            return (ovn_const.OVN_ML2_MECH_DRIVER_NAME in
                    cfg.CONF.ml2.mechanism_drivers)
        except cfg.NoSuchOptError:
            return False

    @registry.receives(resources.TRUNK_PLUGIN, [events.AFTER_INIT])
    def register(self, resource, event, trigger, payload=None):
        super(OVNTrunkDriver, self).register(
            resource, event, trigger, payload=payload)
        self._handler = OVNTrunkHandler(self.plugin_driver)
        for _event in (events.AFTER_CREATE, events.AFTER_UPDATE,
                       events.AFTER_DELETE, events.PRECOMMIT_CREATE,
                       events.PRECOMMIT_DELETE):
            registry.subscribe(self._handler.trunk_event,
                               resources.TRUNK,
                               _event)

        for _event in (events.AFTER_CREATE, events.AFTER_DELETE):
            registry.subscribe(self._handler.subport_event,
                               resources.SUBPORTS,
                               _event)

    @classmethod
    def create(cls, plugin_driver):
        cls.plugin_driver = plugin_driver
        return cls(ovn_const.OVN_ML2_MECH_DRIVER_NAME,
                   SUPPORTED_INTERFACES,
                   SUPPORTED_SEGMENTATION_TYPES,
                   None,
                   can_trunk_bound_port=True)
