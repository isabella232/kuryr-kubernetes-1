# Copyright (c) 2016 Mirantis, Inc.
# All Rights Reserved.
#
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

import os
import signal
import sys

import os_vif
from oslo_config import cfg
from oslo_log import log as logging
from oslo_serialization import jsonutils

from kuryr_kubernetes.cni import api as cni_api
from kuryr_kubernetes.cni.plugins import k8s_cni
from kuryr_kubernetes.cni import utils
from kuryr_kubernetes import config
from kuryr_kubernetes import constants as k_const
from kuryr_kubernetes import objects as k_objects

CONF = cfg.CONF
LOG = logging.getLogger(__name__)
_CNI_TIMEOUT = 180


def run():
    # REVISIT(ivc): current CNI implementation provided by this package is
    # experimental and its primary purpose is to enable development of other
    # components (e.g. functional tests, service/LBaaSv2 support)
    cni_conf = utils.CNIConfig(jsonutils.load(sys.stdin))
    args = ['--config-file', cni_conf.kuryr_conf]

    try:
        if cni_conf.debug:
            args.append('-d')
    except AttributeError:
        pass
    config.init(args)
    config.setup_logging()

    # Initialize o.vo registry.
    k_objects.register_locally_defined_vifs()
    os_vif.initialize()

    if CONF.cni_daemon.daemon_enabled:
        runner = cni_api.CNIDaemonizedRunner()
    else:
        runner = cni_api.CNIStandaloneRunner(k8s_cni.K8sCNIPlugin())
    LOG.info("Using '%s' ", runner.__class__.__name__)

    def _timeout(signum, frame):
        runner._write_dict(sys.stdout, {
            'msg': 'timeout',
            'code': k_const.CNI_TIMEOUT_CODE,
        })
        LOG.debug('timed out')
        sys.exit(1)

    signal.signal(signal.SIGALRM, _timeout)
    signal.alarm(_CNI_TIMEOUT)
    status = runner.run(os.environ, cni_conf, sys.stdout)
    LOG.debug("Exiting with status %s", status)
    if status:
        sys.exit(status)
