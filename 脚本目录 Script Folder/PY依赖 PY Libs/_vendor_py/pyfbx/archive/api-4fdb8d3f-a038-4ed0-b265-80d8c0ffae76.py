"""
Module dedicated to API spec
"""
from collections import namedtuple
from . import utils


_DISC_HTTP_URL = "http://mafreebox.freebox.fr"
_DISC_MDNS_NAME = "_fbx-api._tcp.local."


class _M(namedtuple("Method", ('url', 'doc', 'args'), defaults=([],))):
    """
    Accessor class for API methods
    """
    __slots__ = ()

    @property
    def http_method(self):
        return self.url.split()[0]

    @property
    def endpoint(self):
        return self.url.split()[1]

    def __str__(self):
        return "%s\n\nUrl parameters: %s\nPost data:%s" % (
            self.doc, ",".join(utils.get_params(self.endpoint)), ",".join(self.args))


SYSTEMS = {
    "Download": {
        "Retrieve_a_Download_task": _M("GET downloads/", "Retrieve a Download task"),
        "Delete_a_Download_task": _M("DELETE downloads/{id}", "Delete a Download task"),
        "Update_a_Download_task": _M("PUT downloads/{id}", "Update a Download task", ["PostData"]),
        "Get_download_log": _M("GET downloads/{id}/log", "Get download log"),
        "Adding_a_new_Download_task": _M("POST downloads/add", "Adding a new Download task", ["PostData"]),
    },
    "Download_Feeds": {
        "Get_the_list_of_all_download_Feeds": _M("GET downloads/feeds/", "Get the list of all download Feeds"),
        "Get_a_download_Feed": _M("GET downloads/feeds/{id}", "Get a download Feed"),
        "Add_a_Download_Feed": _M("POST downloads/feeds/", "Add a Download Feed", ["PostData"]),
        "Delete_Download_Feed": _M("DELETE downloads/feeds/{id}", "Delete Download Feed"),
        "Update_a_Download_Feed": _M("PUT downloads/feeds/{id}", "Update a Download Feed", ["PostData"]),
        "Refresh_a_Download_Feed": _M("POST downloads/feeds/{id}/fetch", "Refresh a Download Feed", ["PostData"]),
        "Refresh_all_Download_Feeds": _M("POST downloads/feeds/fetch", "Refresh all Download Feeds", ["PostData"]),
        "Get_the_items_of_a_given_RSS_feed": _M("GET downloads/feeds/{feed_id}/items/", "Get the items of a given RSS feed"),
        "Update_a_feed_item": _M("PUT downloads/feeds/{feed_id}/items/{item_id}", "Update a feed item", ["PostData"]),
        "Download_a_feed_item": _M("POST downloads/feeds/{feed_id}/items/{item_id}/download", "Download a feed item", ["PostData"]),
        "Mark_all_items_as_read": _M("POST downloads/feeds/{feed_id}/items/mark_all_as_read", "Mark all items as read", ["PostData"]),
    },
    "Download_Config": {
        "Updating_the_download_config": _M("PUT downloads/config/", "Updating the Download Config", ["PostData"]),
        "Updating_the_current_Throttling_mode": _M("PUT downloads/throttling", "Updating the current Throttling mode", ["PostData"]),
    },
    "Fs": {
        "List_every_tasks": _M("GET fs/tasks/", "List every tasks"),
        "List_a_task": _M("GET fs/tasks/{id}", "List a task"),
        "Delete_a_task": _M("DELETE fs/tasks/{id}", "Delete a task"),
        "Update_a_task": _M("PUT fs/tasks/{id}", "Update a task", ["PostData"]),
        "List_files": _M("GET fs/ls/{path}", "List files"),
        "Get_file_information": _M("GET fs/info/{path}", "Get file information"),
        "Conflict_resolution": _M("POST fs/mv/", "Conflict resolution", ["PostData"]),
        "Move_files": _M("POST fs/mv/", "Move files", ["PostData"]),
        "Copy_files": _M("POST fs/cp/", "Copy files", ["PostData"]),
        "Remove_files": _M("POST fs/rm/", "Remove files", ["PostData"]),
        "Cat_files": _M("POST fs/cat/", "Cat files", ["PostData"]),
        "Create_an_archive": _M("POST fs/archive/", "Create an archive", ["PostData"]),
        "Extract_a_file": _M("POST fs/extract/", "Extract a file", ["PostData"]),
        "Repair_a_file": _M("POST fs/repair/", "Repair a file", ["PostData"]),
        "Hash_a_file": _M("POST fs/hash/", "Hash a file", ["PostData"]),
        "Create_a_directory": _M("POST fs/mkdir/", "Create a directory", ["PostData"]),
        "Rename_a_file_folder": _M("POST fs/rename/", "Rename a file/folder", ["PostData"]),
        "Download_a_file": _M("GET dl/{path}", "Download a file"),
    },
    "Share": {
        "Retrieve_a_File_Sharing_link": _M("GET share_link/", "Retrieve a File Sharing link"),
        "Delete_a_File_Sharing_link": _M("DELETE share_link/{token}", "Delete a File Sharing link"),
        "Create_a_File_Sharing_link": _M("POST share_link/", "Create a File Sharing link", ["PostData"]),
    },
    "Upload": {
        "File_Upload_example": _M("GET ws/upload", "File Upload example"),
        "Get_the_list_of_uploads": _M("GET upload/", "Get the list of uploads"),
        "Track_an_upload_status": _M("GET upload/{id}", "Track an upload status"),
        "Cancel_an_upload": _M("DELETE upload/{id}/cancel", "Cancel an upload"),
        "Delete_an_upload": _M("DELETE upload/{id}", "Delete an upload"),
        "Cleanup_all_terminated_uploads": _M("DELETE upload/clean", "Cleanup all terminated uploads"),
    },
    "Airmedia": {
        "Get_the_current_AirMedia_configuration": _M("GET airmedia/config/", "Get the current AirMedia configuration"),
        "Update_the_current_AirMedia_configuration": _M("PUT airmedia/config/", "Update the current AirMedia configuration", ["PostData"]),
        "Get_the_list_of_available_AirMedia_receivers": _M("GET airmedia/receivers/", "Get the list of available AirMedia receivers"),
    },
    "Rrd": {
    },
    "Call": {
        "List_every_calls": _M("GET call/log/", "List every calls"),
        "Delete_every_calls": _M("POST call/log/delete_all/", "Delete every calls", ["PostData"]),
        "Mark_every_calls_as_read": _M("POST call/log/mark_all_as_read/", "Mark every calls as read", ["PostData"]),
        "Access_a_given_call_entry": _M("GET call/log/{id}", "Access a given call entry"),
        "Delete_a_call": _M("DELETE call/log/{id}", "Delete a call"),
        "Update_a_call_entry": _M("PUT call/log/{id}", "Update a call entry", ["PostData"]),
    },
    "Contacts": {
        "Get_a_list_of_contacts": _M("GET contact/", "Get a list of contacts"),
        "Access_a_given_contact_entry": _M("GET contact/{id}", "Access a given contact entry"),
        "Create_a_contact": _M("POST contact/", "Create a contact", ["PostData"]),
        "Delete_a_contact": _M("DELETE contact/{id}", "Delete a contact"),
        "Update_a_contact_entry": _M("PUT contact/{id}", "Update a contact entry", ["PostData"]),
        "Get_the_list_of_numbers_for_a_given_contact": _M("GET contact/{contact_id}/[numbers|addresses|urls|emails]/", "Get the list of numbers for a given contact"),
        "Access_a_given_contact_number": _M("GET [number,address,url,email]/{id}", "Access a given contact number"),
        "Create_a_contact_number": _M("POST [number,address,url,email]/", "Create a contact number", ["PostData"]),
        "Delete_a_contact_number": _M("DELETE [number,address,url,email]/{id}", "Delete a contact number"),
        "Update_a_contact_number": _M("PUT [number,address,url,email]/{id}", "Update a contact number", ["PostData"]),
    },
    "Connection": {
        "Get_the_current_Connection_status": _M("GET connection/", "Get the current Connection status"),
        "Get_the_current_Connection_configuration": _M("GET connection/config/", "Get the current Connection configuration"),
        "Update_the_Connection_configuration": _M("PUT connection/config/", "Update the Connection configuration", ["PostData"]),
        "Get_the_current_IPv6_Connection_configuration": _M("GET connection/ipv6/config/", "Get the current IPv6 Connection configuration"),
        "Update_the_IPv6_Connection_configuration": _M("PUT connection/ipv6/config/", "Update the IPv6 Connection configuration", ["PostData"]),
        "Get_the_current_xDSL_infos": _M("GET connection/xdsl/", "Get the current xDSL infos"),
        "Get_the_current_FTTH_status": _M("GET connection/ftth/", "Get the current FTTH status"),
        "Get_the_status_of_a_DynDNS_service": _M("GET connection/ddns/{provider}/status/", "Get the status of a DynDNS service"),
        "Get_the_config_of_a_DynDNS_service": _M("GET connection/ddns/{provider}/", "Get the config of a DynDNS service"),
        "Set_the_config_of_a_DynDNS_service": _M("PUT connection/ddns/{provider}/", "Set the config of a DynDNS service", ["PostData"]),
    },
    "Lan": {
        "Get_the_current_Lan_configuration": _M("GET lan/config/", "Get the current Lan configuration"),
        "Update_the_current_Lan_configuration": _M("PUT lan/config/", "Update the current Lan configuration", ["PostData"]),
    },
    "Freeplug": {
        "Get_the_current_Freeplugs_networks": _M("GET freeplug/", "Get the current Freeplugs networks"),
        "Get_a_particular_Freeplug_information": _M("GET freeplug/{id}/", "Get a particular Freeplug information"),
        "Reset_a_Freeplug": _M("POST freeplug/{id}/reset/", "Reset a Freeplug"),
    },
    "Dhcp": {
        "Get_the_current_DHCP_configuration": _M("GET dhcp/config/", "Get the current DHCP configuration"),
        "Update_the_current_DHCP_configuration": _M("PUT dhcp/config/", "Update the current DHCP configuration", ["PostData"]),
        "Get_the_list_of_DHCP_static_leases": _M("GET dhcp/static_lease/", "Get the list of DHCP static leases"),
        "Get_a_given_DHCP_static_lease": _M("GET dhcp/static_lease/{id}", "Get a given DHCP static lease"),
        "Update_DHCP_static_lease": _M("PUT dhcp/static_lease/{id}", "Update DHCP static lease", ["PostData"]),
        "Delete_a_DHCP_static_lease": _M("DELETE dhcp/static_lease/{id}", "Delete a DHCP static lease"),
        "Add_a_DHCP_static_lease": _M("POST dhcp/static_lease/", "Add a DHCP static lease", ["PostData"]),
        "Get_the_list_of_DHCP_dynamic_leases": _M("GET dhcp/dynamic_lease/", "Get the list of DHCP dynamic leases"),
    },
    "Ftp": {
        "Get_the_current_Ftp_configuration": _M("GET ftp/config/", "Get the current Ftp configuration"),
        "Update_the_FTP_configuration": _M("PUT ftp/config/", "Update the FTP configuration", ["PostData"]),
    },
    "Nat": {
        "Get_the_current_Dmz_configuration": _M("GET fw/dmz/", "Get the current Dmz configuration"),
        "Update_the_current_Dmz_configuration": _M("PUT fw/dmz/", "Update the current Dmz configuration", ["PostData"]),
    },
    "Igd": {
        "Get_the_current_UPnP_IGD_configuration": _M("GET upnpigd/config/", "Get the current UPnP IGD configuration"),
        "Update_the_UPnP_IGD_configuration": _M("PUT upnpigd/config/", "Update the UPnP IGD configuration", ["PostData"]),
        "Get_the_list_of_current_redirection": _M("GET upnpigd/redir/", "Get the list of current redirection"),
        "Delete_a_redirection": _M("DELETE upnpigd/redir/{id}", "Delete a redirection"),
    },
    "Lcd": {
        "Get_the_current_LCD_configuration": _M("GET lcd/config/", "Get the current LCD configuration"),
        "Update_the_lcd_configuration": _M("PUT lcd/config/", "Update the lcd configuration", ["PostData"]),
    },
    "Network_Share": {
        "Get_the_current_Samba_configuration": _M("GET netshare/samba/", "Get the current Samba configuration"),
        "Update_the_Samba_configuration": _M("PUT netshare/samba/", "Update the Samba configuration", ["PostData"]),
        "Get_the_current_Afp_configuration": _M("GET netshare/afp/", "Get the current Afp configuration"),
        "Update_the_Afp_configuration": _M("PUT netshare/afp/", "Update the Afp configuration", ["PostData"]),
    },
    "Upnpav": {
        "Get_the_current_UPnP_AV_configuration": _M("GET upnpav/config/", "Get the current UPnP AV configuration"),
        "Update_the_UPnP_AV_configuration": _M("PUT upnpav/config/", "Update the UPnP AV configuration", ["PostData"]),
    },
    "Switch": {
        "Get_the_current_switch_status": _M("GET switch/status/", "Get the current switch status"),
        "Get_a_port_configuration": _M("GET switch/port/{id}", "Get a port configuration"),
        "Update_a_port_configuration": _M("PUT switch/port/{id}", "Update a port configuration", ["PostData"]),
        "Get_a_port_stats": _M("GET switch/port/{id}/stats", "Get a port stats"),
    },
    "Wifi": {
        "Get_the_current_WiFi_global_configuration": _M("GET wifi/config/", "Get the current Wi-Fi global configuration"),
        "Update_the_WiFi_global_configuration": _M("PUT wifi/config/", "Update the Wi-Fi global configuration", ["PostData"]),
        "WiFi_AP_API": _M("GET wifi/ap/", "Wi-Fi AP API"),
        "Get_WiFi_Stations_List": _M("GET wifi/ap/{id}/stations/", "Get Wi-Fi Stations List"),
        "WiFi_BSS_API": _M("GET wifi/bss/", "Wi-Fi BSS API"),
        "List_AP_neighbors": _M("GET wifi/ap/{id}/neighbors/", "List AP neighbors"),
        "List_WiFi_channels_usage": _M("GET wifi/ap/{id}/channel_usage/", "List Wi-Fi channels usage"),
        "Refresh_radar_informations": _M("POST wifi/ap/{id}/neighbors/scan", "Refresh radar informations", ["PostData"]),
        "Get_WiFi_Planning": _M("GET wifi/planning/", "Get Wi-Fi Planning"),
        "Update_WiFi_Planning": _M("PUT wifi/planning/", "Update Wi-Fi Planning", ["PostData"]),
        "Get_the_MAC_filter_list": _M("GET wifi/mac_filter/", "Get the MAC filter list"),
        "Getting_a_particular_MAC_filter": _M("GET wifi/mac_filter/{filter_id}", "Getting a particular MAC filter"),
        "Updating_a_MAC_filter": _M("PUT wifi/mac_filter/{filter_id}", "Updating a MAC filter", ["PostData"]),
        "Delete_a_MAC_filter": _M("DELETE wifi/mac_filter/{filter_id}", "Delete a MAC filter"),
        "Create_a_new_MAC_filter": _M("POST wifi/mac_filter/", "Create a new MAC filter", ["PostData"]),
    },
    "System": {
        "Get_the_current_system_info": _M("GET system/", "Get the current system info [UNSTABLE]"),
        "Reboot_the_system": _M("POST system/reboot/", "Reboot the system"),
    },
    "Vpn": {
        "VPN_Server_List_API": _M("GET vpn/", "VPN Server List API"),
        "Get_a_VPN_config": _M("GET vpn/{vpn_id}/config/", "Get a VPN config"),
        "Update_the_VPN_configuration": _M("PUT vpn/openvpn_routed/config/", "Update the VPN configuration", ["PostData"]),
        "VPN_Server_User_List": _M("GET vpn/user/", "VPN Server User List"),
        "Get_a_VPN_user": _M("GET vpn/user/{login}", "Get a VPN user"),
        "Add_a_VPN_User": _M("POST vpn/user/", "Add a VPN User", ["PostData"]),
        "Delete_a_VPN_User": _M("DELETE vpn/user/{login}", "Delete a VPN User"),
        "Update_a_VPN_User": _M("PUT vpn/user/{login}", "Update a VPN User", ["PostData"]),
        "Get_the_VPN_server_IP_pool_reservations": _M("GET vpn/ip_pool/", "Get the VPN server IP pool reservations"),
        "Get_the_list_of_connections": _M("GET vpn/connection/", "Get the list of connections"),
        "Close_a_given_connection": _M("DELETE vpn/connection/{id}", "Close a given connection"),
        "Donwload_a_user_configuration_file": _M("GET vpn/download_config/{server_name}/{login}", "Donwload a user configuration file"),
    },
    "Vpn_Client": {
        "Get_VPN_Client_configuration_list": _M("GET vpn_client/config/", "Get VPN Client configuration list"),
        "Get_a_VPN_client_config": _M("GET vpn_client/config/{id}", "Get a VPN client config"),
        "Add_a_VPN_client_configuration": _M("POST vpn_client/config/", "Add a VPN client configuration", ["PostData"]),
        "Delete_a_VPN_client_Configuration": _M("DELETE vpn_client/config/{id}", "Delete a VPN client Configuration"),
        "Update_the_VPN_client_configuration": _M("PUT vpn_client/config/{id}", "Update the VPN client configuration", ["PostData"]),
        "Get_the_VPN_client_status": _M("GET vpn_client/status", "Get the VPN client status"),
        "Get_the_VPN_client_logs": _M("GET vpn_client/log", "Get the VPN client logs"),
    },
    "Storage": {
        "Get_the_list_of_disks": _M("GET storage/disk/", "Get the list of disks"),
        "Get_a_given_disk_info": _M("GET storage/disk/{id}", "Get a given disk info"),
        "Update_a_disk_state": _M("PUT storage/disk/{id}", "Update a disk state", ["PostData"]),
        "Format_a_disk": _M("PUT storage/disk/{id}/format/", "Format a disk", ["PostData"]),
        "Get_the_list_of_partitions": _M("GET storage/partition/", "Get the list of partitions"),
        "Get_a_given_partition_info": _M("GET storage/partition/{id}", "Get a given partition info"),
        "Update_a_partition_state": _M("PUT storage/partition/{id}", "Update a partition state", ["PostData"]),
        "Check_a_partition": _M("PUT storage/partition/{id}/check/", "Check a partition", ["PostData"]),
        "Get_the_current_storage_configuration": _M("GET storage/config/", "Get the current storage configuration"),
        "Update_the_External_Storage_configuration": _M("PUT storage/config/", "Update the External Storage configuration", ["PostData"]),
    },
    "Parental": {
        "Get_parental_filter_config": _M("GET parental/config/", "Get parental filter config"),
        "Update_parental_filter_config": _M("PUT parental/config/", "Update parental filter config", ["PostData"]),
        "Retrieve_all_Parental_Filter_rules": _M("GET parental/filter/", "Retrieve all Parental Filter rules"),
        "Retrieve_a_Parental_Filter_rule": _M("GET parental/filter/{id}", "Retrieve a Parental Filter rule"),
        "Delete_a_Parental_Filter_rule": _M("DELETE parental/filter/{id}", "Delete a Parental Filter rule"),
        "Update_a_Parental_Filter_rule": _M("PUT parental/filter/{id}", "Update a Parental Filter rule", ["PostData"]),
        "Add_a_Parental_Filter_rule": _M("POST parental/filter/", "Add a Parental Filter rule", ["PostData"]),
        "Get_a_parental_filter_Planning": _M("GET parental/filter/{id}/planning", "Get a parental filter Planning"),
        "Update_a_parental_filter_Planning": _M("PUT parental/filter/{id}/planning", "Update a parental filter Planning", ["PostData"]),
    },
    "Pvr": {
        "Get_the_current_PVR_configuration": _M("GET pvr/config/", "Get the current PVR configuration"),
        "Update_the_current_PVR_configuration": _M("PUT pvr/config/", "Update the current PVR configuration", ["PostData"]),
        "Getting_the_current_quota_info": _M("GET pvr/quota/", "Getting the current quota info"),
        "Request_next_quota_threshold": _M("PUT pvr/quota/", "Request next quota threshold", ["PostData"]),
    },
}
