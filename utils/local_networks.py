"""Networks treated as local in administrator access-log views and PV reports."""

from ipaddress import IPv4Network, IPv6Network, ip_address, ip_network


LOCAL_NETS = (
    "127.0.0.1",
    "::1",
    "192.168.103.0/24",
    "2404:7a81:bc40:2a00::/64",
    "2404:7a81:8ac1:1000::/64",
)
LOCAL_NETS_OBJ = tuple(ip_network(value) for value in LOCAL_NETS)


def is_local_ip(value: str) -> bool:
    try:
        address = ip_address(value)
    except ValueError:
        return False
    return any(address in network for network in LOCAL_NETS_OBJ)


def sql_local_ip_predicate(column: str = "ip") -> tuple[str, tuple[str, ...]]:
    """Return a parameterized predicate equivalent for canonical access-log IPs.

    All configured CIDRs are exact addresses or /8-aligned IPv4 and
    /16-aligned IPv6 prefixes, so LIKE prefixes cannot include adjacent nets.
    ``column`` must be a fixed, code-owned SQL identifier.
    """
    clauses = []
    values = []
    for network in LOCAL_NETS_OBJ:
        if network.num_addresses == 1:
            clauses.append(f"{column} = %s")
            values.append(str(network.network_address))
        elif isinstance(network, IPv4Network) and network.prefixlen % 8 == 0:
            octets = str(network.network_address).split(".")[:network.prefixlen // 8]
            clauses.append(f"{column} LIKE %s")
            values.append(".".join(octets) + ".%")
        elif isinstance(network, IPv6Network) and network.prefixlen % 16 == 0:
            hextets = network.network_address.exploded.split(":")[:network.prefixlen // 16]
            clauses.append(f"{column} LIKE %s")
            values.append(":".join(format(int(value, 16), "x") for value in hextets) + ":%")
        else:
            raise ValueError("SQL predicate needs a hextet/octet-aligned local network")
    return "(" + " OR ".join(clauses) + ")", tuple(values)
