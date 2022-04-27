# Author: Julien Castets <castets.j@gmail.com>
#
# This file is part of cloud-init. See LICENSE file for license information.

# Scaleway API:
# https://developer.scaleway.com/#metadata

import json
import os
import socket
import time

import requests

# pylint fails to import the two modules below.
# These are imported via requests.packages rather than urllib3 because:
#  a.) the provider of the requests package should ensure that urllib3
#      contained in it is consistent/correct.
#  b.) cloud-init does not specifically have a dependency on urllib3
#
# For future reference, see:
#   https://github.com/kennethreitz/requests/pull/2375
#   https://github.com/requests/requests/issues/4104
# pylint: disable=E0401
from requests.packages.urllib3.connection import HTTPConnection
from requests.packages.urllib3.poolmanager import PoolManager

from cloudinit import dmi
from cloudinit import log as logging
from cloudinit import net, sources, url_helper, subp, util
from cloudinit.event import EventScope, EventType
from cloudinit.net import activators, read_sys_net_int
from cloudinit.net.dhcp import EphemeralDHCPv4, NoDHCPLeaseError

LOG = logging.getLogger(__name__)

DS_BASE_URL = "http://169.254.42.42"
SCW_METADATA_AUTH_TOKEN = "X-Metadata-Auth-Token"

BUILTIN_DS_CONFIG = {
    "metadata_url": DS_BASE_URL + "/conf?format=json",
    "userdata_url": DS_BASE_URL + "/user_data/cloud-init",
    "vendordata_url": DS_BASE_URL + "/vendor_data/cloud-init",
}

DEF_MD_RETRIES = 5
DEF_MD_TIMEOUT = 10


def on_scaleway():
    """
    There are three ways to detect if you are on Scaleway:

    * check DMI data: not yet implemented by Scaleway, but the check is made to
      be future-proof.
    * the initrd created the file /var/run/scaleway.
    * "scaleway" is in the kernel cmdline.
    """
    vendor_name = dmi.read_dmi_data("system-manufacturer")
    if vendor_name == "Scaleway":
        return True

    if os.path.exists("/var/run/scaleway"):
        return True

    cmdline = util.get_cmdline()
    if "scaleway" in cmdline:
        return True

    return False


class SourceAddressAdapter(requests.adapters.HTTPAdapter):
    """
    Adapter for requests to choose the local address to bind to.
    """

    def __init__(self, source_address, **kwargs):
        self.source_address = source_address
        super(SourceAddressAdapter, self).__init__(**kwargs)

    def init_poolmanager(self, connections, maxsize, block=False):
        socket_options = HTTPConnection.default_socket_options + [
            (socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        ]
        self.poolmanager = PoolManager(
            num_pools=connections,
            maxsize=maxsize,
            block=block,
            source_address=self.source_address,
            socket_options=socket_options,
        )


def query_data_api_once(api_address, headers, headers_redact, timeout, requests_session):
    """
    Retrieve user data or vendor data.

    Scaleway user/vendor data API returns HTTP/404 if user/vendor data is not
    set.

    This function calls `url_helper.readurl` but instead of considering
    HTTP/404 as an error that requires a retry, it considers it as empty
    user/vendor data.

    Also, be aware the user data/vendor API requires the source port to be
    below 1024 to ensure the client is root (since non-root users can't bind
    ports below 1024). If requests raises ConnectionError (EADDRINUSE), the
    caller should retry to call this function on an other port.
    """
    try:
        resp = url_helper.readurl(
            api_address,
            data=None,
            headers=headers,
            headers_redact=headers_redact,
            timeout=timeout,
            # It's the caller's responsability to recall this function in case
            # of exception. Don't let url_helper.readurl() retry by itself.
            retries=0,
            session=requests_session,
            # If the error is a HTTP/404 or a ConnectionError, go into raise
            # block below and don't bother retrying.
            exception_cb=lambda _, exc: exc.code != 404
            and (
                not isinstance(exc.cause, requests.exceptions.ConnectionError)
            ),
        )
        return util.decode_binary(resp.contents)
    except url_helper.UrlError as exc:
        # Empty user data.
        if exc.code == 404:
            return None
        raise


def query_data_api(api_type, api_address, headers, headers_redact, retries, timeout):
    """Get user or vendor data.

    Handle the retrying logic in case the source port is used.

    Scaleway metadata service requires the source port of the client to
    be a privileged port (<1024).  This is done to ensure that only a
    privileged user on the system can access the metadata service.
    """
    # Query user/vendor data. Try to make a request on the first privileged
    # port available.
    for port in range(1, max(retries, 2)):
        try:
            LOG.debug(
                "Trying to get %s data (bind on port %d)...", api_type, port
            )
            requests_session = requests.Session()
            requests_session.mount(
                "http://",
                SourceAddressAdapter(source_address=("0.0.0.0", port)),
            )
            data = query_data_api_once(
                api_address, headers=headers, headers_redact=headers_redact, timeout=timeout, requests_session=requests_session
            )
            LOG.debug("%s-data downloaded", api_type)
            return data

        except url_helper.UrlError as exc:
            # Local port already in use or HTTP/429.
            LOG.warning("Error while trying to get %s data: %s", api_type, exc)
            time.sleep(5)
            last_exc = exc
            continue

    # Max number of retries reached.
    raise last_exc


class DataSourceScaleway(sources.DataSource):
    dsname = "Scaleway"
    default_update_events = {
        EventScope.NETWORK: {
            EventType.BOOT_NEW_INSTANCE,
            EventType.BOOT,
            EventType.BOOT_LEGACY,
        }
    }

    def __init__(self, sys_cfg, distro, paths):
        super(DataSourceScaleway, self).__init__(sys_cfg, distro, paths)

        self.ds_cfg = util.mergemanydict(
            [
                util.get_cfg_by_path(sys_cfg, ["datasource", "Scaleway"], {}),
                BUILTIN_DS_CONFIG,
            ]
        )

        self.metadata_address = self.ds_cfg.get("custom_metadata_url", "{base_url}/conf?format=json".format(base_url=self.ds_cfg.get("base_url", DS_BASE_URL)))
        self.userdata_address = self.ds_cfg.get("custom_userdata_url", "{base_url}/user_data/cloud-init".format(base_url=self.ds_cfg.get("base_url", DS_BASE_URL)))
        self.vendordata_address = self.ds_cfg.get("custom_vendordata_url", "{base_url}/vendor_data/cloud-init".format(base_url=self.ds_cfg.get("base_url", DS_BASE_URL)))
        self.headers_redact = None
        self.headers = None
        # Scaleway Baremetal product use X-Metadata-Auth-Token
        authToken = self.ds_cfg.get("token", None)
        if authToken is not None:
            self.headers_redact = SCW_METADATA_AUTH_TOKEN
            self.headers = {SCW_METADATA_AUTH_TOKEN: authToken}

        self.retries = int(self.ds_cfg.get("retries", DEF_MD_RETRIES))
        self.timeout = int(self.ds_cfg.get("timeout", DEF_MD_TIMEOUT))
        self._network_config = sources.UNSET

    @property
    def fallback_interface(self):
        if self._fallback_interface is None:
            self._fallback_interface = get_first_connected_interface()
            if self._fallback_interface is None:
                LOG.warning(
                    "Did not find a fallback interface on %s.", self.cloud_name
                )
        return self._fallback_interface

    def _crawl_metadata(self):
        # Stay backward compatible with classes w/o these attributes
        self.headers = getattr(self, 'headers', None)
        self.headers_redact = getattr(self, 'headers_redact', None)

        resp = url_helper.readurl(
            self.metadata_address, headers=self.headers, headers_redact=self.headers_redact, timeout=self.timeout, retries=self.retries
        )
        self.metadata = json.loads(util.decode_binary(resp.contents))

        self.userdata_raw = query_data_api(
            "user-data", self.userdata_address, self.headers, self.headers_redact, self.retries, self.timeout
        )
        self.vendordata_raw = query_data_api(
            "vendor-data", self.vendordata_address, self.headers, self.headers_redact, self.retries, self.timeout
        )

    def _get_data(self):
        if not on_scaleway():
            return False

        if self._fallback_interface is None:
            self._fallback_interface = get_first_connected_interface()
        try:
            with EphemeralDHCPv4(self._fallback_interface):
                util.log_time(
                    logfunc=LOG.debug,
                    msg="Crawl of metadata service",
                    func=self._crawl_metadata,
                )
        except (NoDHCPLeaseError) as e:
            util.logexc(LOG, str(e))
            return False
        return True

    @property
    def network_config(self):
        """
        Configure networking according to data received from the
        metadata API.
        """
        if self._network_config is None:
            LOG.warning(
                "Found None as cached _network_config. Resetting to %s",
                sources.UNSET,
            )
            self._network_config = sources.UNSET

        if self._network_config != sources.UNSET:
            return self._network_config

        if self._fallback_interface is None:
            self._fallback_interface = get_first_connected_interface()

        netcfg = {"type": "physical", "name": "%s" % self._fallback_interface}
        subnets = [{"type": "dhcp4"}]
        if 'ipv6' in self.metadata:
            if self.metadata["ipv6"]:
                subnets += [
                    {
                        "type": "static",
                        "address": "%s" % self.metadata["ipv6"]["address"],
                        "gateway": "%s" % self.metadata["ipv6"]["gateway"],
                        "netmask": "%s" % self.metadata["ipv6"]["netmask"],
                    }
                ]
        netcfg["subnets"] = subnets
        self._network_config = {"version": 1, "config": [netcfg]}
        return self._network_config

    @property
    def launch_index(self):
        return None

    def get_instance_id(self):
        return self.metadata["id"]

    def get_public_ssh_keys(self):
        ssh_keys = [key["key"] for key in self.metadata["ssh_public_keys"]]

        akeypre = "AUTHORIZED_KEY="
        plen = len(akeypre)
        for tag in self.metadata.get("tags", []):
            if not tag.startswith(akeypre):
                continue
            ssh_keys.append(tag[:plen].replace("_", " "))

        return ssh_keys

    def get_hostname(self, fqdn=False, resolve_ip=False, metadata_only=False):
        return self.metadata["hostname"]

    @property
    def availability_zone(self):
        return None

    @property
    def region(self):
        return None


datasources = [
    (DataSourceScaleway, (sources.DEP_FILESYSTEM,)),
    (DataSourceScaleway, (sources.DEP_FILESYSTEM, sources.DEP_NETWORK)),
]


def get_datasource_list(depends):
    return sources.list_from_depends(depends, datasources)

# Get first connected interface
def get_first_connected_interface():
    for iface in net.find_candidate_nics():
        # ensure interface is UP
        try:
            cmd = ["ip", "link", "set", "dev", iface, "up"]
            LOG.debug("Running command %s ...", cmd)
            subp.subp(cmd, capture=False)
        except ProcessExecutionError as err:
            LOG.debug("command %s failed: %s ... skip to next interface", cmd, err)
            continue

        # ensure interface is connected
        carrier = read_sys_net_int(iface, "carrier")
        if not carrier:
            continue

        return iface

    return None
