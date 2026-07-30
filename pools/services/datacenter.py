"""
Whether a request came from a machine room rather than somebody's home or phone.

Only the verdict is ever kept. The address is looked at in memory to answer one
yes/no question and is never written anywhere — the same rule the user-agent
already lives under, which is read for a family and a bot check and then dropped.

Two things are deliberately left out of the list below:

* CDN, proxy and relay networks — Cloudflare, Fastly, Akamai's edge, Apple's
  iCloud Private Relay. Those carry real people's traffic, and a rule that
  catches them is a rule that erases privacy-conscious visitors.
* Any claim to completeness. This is a hand-written list of the providers that
  actually turn up scraping small sites, so it will always be missing somebody,
  and the ranges shift under it over time. That is survivable because it is one
  signal among several rather than the only line of defence, and because the
  rollup only ever consults it about visitors who never ran the page's
  JavaScript. Missing a range costs a crawler counted as a visitor; that is the
  cheaper mistake, and the one this file should keep erring towards.
"""
import ipaddress

# IPv4 only. Ranges are coarse on purpose: an approximate block that stays right
# for years beats a precise one that silently rots.
_PROVIDER_RANGES = """
# Tencent Cloud
1.12.0.0/14
43.128.0.0/10
49.51.0.0/16
49.232.0.0/14
101.32.0.0/16
119.28.0.0/16
124.156.0.0/16
129.226.0.0/16
150.109.0.0/16
170.106.0.0/16

# Alibaba Cloud
8.208.0.0/12
47.52.0.0/14
47.74.0.0/15
47.76.0.0/14
47.88.0.0/14
47.235.0.0/16
47.236.0.0/14
47.240.0.0/13

# Amazon Web Services
3.0.0.0/8
18.128.0.0/9
34.192.0.0/10
44.192.0.0/10
52.0.0.0/11
54.64.0.0/10
54.144.0.0/12
54.224.0.0/11
99.77.0.0/16
100.20.0.0/14

# Google Cloud (compute ranges only — Google's fetch and prefetch proxies live
# elsewhere, and those are handled by the Sec-Purpose check, not by address)
34.64.0.0/10
35.184.0.0/13
35.192.0.0/11
35.224.0.0/12
35.240.0.0/13

# Microsoft Azure
13.64.0.0/11
20.0.0.0/8
40.64.0.0/10
52.96.0.0/12
104.40.0.0/13

# DigitalOcean
68.183.0.0/16
104.131.0.0/16
128.199.0.0/16
134.209.0.0/16
138.68.0.0/16
142.93.0.0/16
143.110.0.0/16
143.198.0.0/16
146.190.0.0/16
157.230.0.0/16
159.65.0.0/16
159.89.0.0/16
161.35.0.0/16
164.90.0.0/16
165.22.0.0/16
165.227.0.0/16
167.71.0.0/16
167.99.0.0/16
178.62.0.0/16
188.166.0.0/16
206.189.0.0/16
209.97.128.0/18

# Linode / Akamai compute
45.33.0.0/17
45.56.0.0/16
45.79.0.0/16
50.116.0.0/16
66.175.208.0/20
96.126.96.0/19
139.162.0.0/16
172.104.0.0/15
172.232.0.0/13
173.255.192.0/18
192.46.208.0/20
198.58.96.0/19

# Vultr
45.32.0.0/16
45.63.0.0/16
45.76.0.0/16
45.77.0.0/16
64.176.0.0/16
66.42.32.0/19
70.34.192.0/18
95.179.128.0/17
104.156.224.0/19
108.61.0.0/16
136.244.64.0/18
149.28.0.0/16
155.138.128.0/17
199.247.0.0/18
207.148.0.0/18
216.128.128.0/17

# Hetzner
5.9.0.0/16
65.108.0.0/16
65.109.0.0/16
78.46.0.0/15
88.99.0.0/16
94.130.0.0/16
95.216.0.0/15
116.202.0.0/16
128.140.0.0/17
135.181.0.0/16
138.201.0.0/16
142.132.0.0/17
144.76.0.0/16
148.251.0.0/16
157.90.0.0/16
159.69.0.0/16
162.55.0.0/16
167.235.0.0/16
168.119.0.0/16
176.9.0.0/16
178.63.0.0/16
188.34.0.0/16
195.201.0.0/16

# OVH
51.68.0.0/14
51.75.0.0/16
51.77.0.0/16
51.79.0.0/16
51.83.0.0/16
51.89.0.0/16
51.91.0.0/16
51.195.0.0/16
54.36.0.0/16
91.121.0.0/16
137.74.0.0/16
139.99.0.0/16
141.94.0.0/16
145.239.0.0/16
147.135.0.0/16
149.202.0.0/16
151.80.0.0/16
158.69.0.0/16
164.132.0.0/16
167.114.0.0/16
176.31.0.0/16
178.32.0.0/15
188.165.0.0/16
192.99.0.0/16
213.32.0.0/17
217.182.0.0/16

# Scaleway
51.15.0.0/16
51.158.0.0/15
51.159.0.0/16
62.210.0.0/16
163.172.0.0/16
195.154.0.0/16
212.47.224.0/19
212.83.128.0/19
212.129.0.0/18
"""


def _build_index():
    """
    Bucket the ranges by first octet, so a lookup compares against a handful of
    networks rather than all of them. Built once at import.
    """
    index = {}
    for line in _PROVIDER_RANGES.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        net = ipaddress.ip_network(line)
        first = int(net.network_address) >> 24
        last = int(net.broadcast_address) >> 24
        for octet in range(first, last + 1):
            index.setdefault(octet, []).append(net)
    return index


_BY_FIRST_OCTET = _build_index()


def is_datacenter_ip(ip: str) -> bool:
    """
    True if `ip` sits in a hosting provider's range.

    Anything unparseable is False rather than an error: this is measurement, and a
    malformed forwarded-for header must never be the reason a page fails to load.
    IPv6 is not covered yet, and answers False — an honest gap rather than a guess.
    """
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if addr.version != 4:
        return False
    for net in _BY_FIRST_OCTET.get(int(addr) >> 24, ()):
        if addr in net:
            return True
    return False
