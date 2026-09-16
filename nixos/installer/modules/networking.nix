# Installer networking: DHCP on all ethernet, mDNS lemonseed.local.

{ ... }:

{
  networking.useDHCP = true;
  networking.firewall.allowedTCPPorts = [ 80 ];
  networking.firewall.allowedUDPPorts = [
    5353 # mDNS
  ];

  # Advertise http://lemonseed.local/ on all interfaces.
  services.avahi = {
    enable = true;
    nssmdns4 = true;
    openFirewall = true;
    publish = {
      enable = true;
      addresses = true;
      workstation = true;
      domain = true;
      hinfo = true;
      userServices = true;
    };
    extraServiceFiles = {
      installer = ''
        <?xml version="1.0" standalone='no'?>
        <!DOCTYPE service-group SYSTEM "avahi-service.dtd">
        <service-group>
          <name replace-wildcards="yes">Lemonseed Installer on %h</name>
          <service>
            <type>_http._tcp</type>
            <port>80</port>
            <txt-record>path=/</txt-record>
          </service>
        </service-group>
      '';
    };
  };

  # Keep avahi-daemon running as a normal service. The default NixOS
  # configuration is socket-activated: the service has `requires = [ "avahi-daemon.socket" ]`
  # and `Type = "dbus"`, so the daemon exits when the socket goes idle. The
  # installer has no D-Bus client (unlike the appliance's lemonseed-avahi-alias),
  # so the daemon would be HUP'd and mDNS discovery would stop working.
  # Drop the `requires` so the service starts standalone from multi-user.target.
  # (Do NOT disable the socket unit: NixOS `enable = false` masks it, and a
  # masked required unit makes the service fail to start.)
  systemd.services.avahi-daemon.requires = [ ];
}
