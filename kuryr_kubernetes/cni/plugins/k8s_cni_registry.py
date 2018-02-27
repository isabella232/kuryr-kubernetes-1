# Copyright 2017 Red Hat, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import retrying

from os_vif import objects as obj_vif
from os_vif.objects import base
from oslo_concurrency import lockutils
from oslo_config import cfg
from oslo_log import log as logging

from kuryr_kubernetes.cni.binding import base as b_base
from kuryr_kubernetes.cni.plugins import base as base_cni
from kuryr_kubernetes import exceptions

LOG = logging.getLogger(__name__)
CONF = cfg.CONF
RETRY_DELAY = 1000  # 1 second in milliseconds

# TODO(dulek): Another corner case is (and was) when pod is deleted before it's
#              annotated by controller or even noticed by any watcher. Kubelet
#              will try to delete such vif, but we will have no data about it.
#              This is currently worked around by returning succesfully in case
#              of timing out in delete. To solve this properly we need to watch
#              for pod deletes as well.


class K8sCNIRegistryPlugin(base_cni.CNIPlugin):
    def __init__(self, registry, healthy):
        self.healthy = healthy
        self.registry = registry

    def _get_name(self, pod):
        return pod['metadata']['name']

    def add(self, params):
        vif = self._do_work(params, b_base.connect)

        pod_name = params.args.K8S_POD_NAME
        # NOTE(dulek): Saving containerid to be able to distinguish old DEL
        #              requests that we should ignore. We need a lock to
        #              prevent race conditions and replace whole object in the
        #              dict for multiprocessing.Manager to notice that.
        with lockutils.lock(pod_name, external=True):
            d = self.registry[pod_name]
            d['containerid'] = params.CNI_CONTAINERID
            self.registry[pod_name] = d
            LOG.debug('Saved containerid = %s for pod %s',
                      params.CNI_CONTAINERID, pod_name)

        # Wait for VIF to become active.
        timeout = CONF.cni_daemon.vif_annotation_timeout

        # Wait for timeout sec, 1 sec between tries, retry when vif not active.
        @retrying.retry(stop_max_delay=timeout * 1000, wait_fixed=RETRY_DELAY,
                        retry_on_result=lambda x: not x.active)
        def wait_for_active(pod_name):
            return base.VersionedObject.obj_from_primitive(
                self.registry[pod_name]['vif'])

        vif = wait_for_active(pod_name)
        if not vif.active:
            raise exceptions.ResourceNotReady(pod_name)

        return vif

    def delete(self, params):
        pod_name = params.args.K8S_POD_NAME
        try:
            reg_ci = self.registry[pod_name]['containerid']
            LOG.debug('Read containerid = %s for pod %s', reg_ci, pod_name)
            if reg_ci and reg_ci != params.CNI_CONTAINERID:
                # NOTE(dulek): This is a DEL request for some older (probably
                #              failed) ADD call. We should ignore it or we'll
                #              unplug a running pod.
                LOG.warning('Received DEL request for unknown ADD call. '
                            'Ignoring.')
                return
        except KeyError:
            pass
        self._do_work(params, b_base.disconnect)

    def report_drivers_health(self, driver_healthy):
        if not driver_healthy:
            with self.healthy.get_lock():
                LOG.debug("Reporting CNI driver not healthy.")
                self.healthy.value = driver_healthy

    def _do_work(self, params, fn):
        pod_name = params.args.K8S_POD_NAME

        timeout = CONF.cni_daemon.vif_annotation_timeout

        # In case of KeyError retry for `timeout` s, wait 1 s between tries.
        @retrying.retry(stop_max_delay=timeout * 1000, wait_fixed=RETRY_DELAY,
                        retry_on_exception=lambda e: isinstance(e, KeyError))
        def find():
            return self.registry[pod_name]

        try:
            d = find()
            pod = d['pod']
            vif = base.VersionedObject.obj_from_primitive(d['vif'])
        except KeyError:
            raise exceptions.ResourceNotReady(pod_name)

        fn(vif, self._get_inst(pod), params.CNI_IFNAME, params.CNI_NETNS,
           self.report_drivers_health)
        return vif

    def _get_inst(self, pod):
        return obj_vif.instance_info.InstanceInfo(
            uuid=pod['metadata']['uid'], name=pod['metadata']['name'])
