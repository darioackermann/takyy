# pylint: disable=missing-module-docstring
import time
import enum
import logging
from datetime import datetime as dt
from datetime import timedelta

from taky.config import app_config
from . import models
from .client import TAKClient
from .persistence import build_persistence


class Destination(enum.Enum):
    """
    Indicate where this packet is routed
    """

    BROADCAST = 1
    GROUP = 2


class COTRouter:
    """
    A class to keep track of clients, and ensure packets get routed properly.
    """

    def __init__(self):
        # TODO: self.clients as dictionary, with UID as keys?
        #     : should prohibit multiple sockets sharing a client
        self.clients = set()
        self.persist = build_persistence()
        self.last_prune = 0
        self.max_ttl = app_config.getint("cot_server", "max_persist_ttl")
        self.allowed_connections = eval(app_config.get("custom", "uid_to_ip"))
        print(self.allowed_connections)
        self.lgr = logging.getLogger(self.__class__.__name__)
        self.ip_to_uid = {}

    def prune(self):
        now = time.time()
        if (now - self.last_prune) > 10:
            self.last_prune = now
            self.persist.prune()

    def client_connect(self, client):
        """
        Add a client to the router
        """
        #print(type(client.sock))
        ip, port = client.sock.getpeername()
        print(f"Client from IP {ip} connected")
        self.clients.add(client)

    def client_disconnect(self, client):
        """
        Remove a client from the router
        """
        self.clients.discard(client)

    def send_persist(self, client):
        """
        Called by TAKClient when the client first identifies to the server
        """
        self.lgr.debug("Sending persistence objects to %s", client)
        for event in self.persist.get_all():
            if client.user and event.uid == client.user.uid:
                continue

            client.send_event(event)

    def find_clients(self, uid=None, callsign=None):
        """
        Returns an iterator of objects matching the criteria
        """
        for client in self.clients:
            if not client.user:
                continue

            if uid and client.user.uid == uid:
                yield client
            if callsign and client.user.callsign == callsign:
                yield client

    def broadcast(self, src, msg):
        """
        Broadcast a message from source to all clients
        """
        if src.user:
            self.lgr.debug("%s -> Broadcast: %s", src.user.callsign, msg)
        else:
            self.lgr.debug("Anonymous Broadcast: %s", msg)

        self.persist.track(msg)
        for client in self.clients:
            if client is src:
                continue

            client.send_event(msg)

    def group_broadcast(self, src, msg, group=None):
        """
        Broadcast a message from source to all members to a group.

        If group is not specified, the source's group is used.
        """
        if isinstance(src, TAKClient):
            src = src.user

        if group is None:
            if src is None:
                raise ValueError("Unable to determine group to send to")
            group = src.group

        if not isinstance(group, models.Teams):
            raise ValueError("group must be models.Teams")

        if src:
            self.lgr.debug("%s -> %s: %s", src.callsign, group, msg)
        else:
            self.lgr.debug("Anonymous -> %s: %s", group, msg)

        for client in self.clients:
            if not client.user or (client.user is src):
                continue

            if client.user.group == group:
                client.send_event(msg)

    def send_user(self, src, msg, dst_cs=None, dst_uid=None):
        """
        Send a message to a destination by callsign or UID
        """
        for client in self.find_clients(uid=dst_uid, callsign=dst_cs):
            client.send_event(msg)

    def route(self, src, evt):
        """
        Push an event to the router
        """
        # NOTES
        # src should be a client object with sock attribute, from which the IP can be read
        # Then evt should contain message and also the target callsign or something
        # Actually the msg might not contain a callsign, rather the src is either anonymous or has identified itself with a callsign earlier
        
        if not isinstance(evt, models.Event):
            raise ValueError(f"Unable to route {type(evt)}")
        
        # TODO: Remove, can't actually completely block anon traffic
        #if isinstance(src, TAKClient) and src.user is None or src.user.callsign is None:
        #    print(f"Routing {evt}, despite {src} no callsign")
        #    #return
        #else:
        #    print(f"Routing {evt} from {src}")
        
        try:
            client_ip = src.sock.getpeername()[0]
            message_uid = evt.uid
            if message_uid[:7] != "bridge-":
                if client_ip not in self.ip_to_uid:
                    self.ip_to_uid[client_ip] = set()
                self.ip_to_uid[client_ip].add(message_uid)
                if len(self.ip_to_uid[client_ip]) > 1:
                    print(f"IP {client_ip} has sent multiple uid-s. Ignoring everything from that IP")
                    print()
                    return
        except:
            pass
    
            
        try:
            client_ip = src.sock.getpeername()[0]
            message_uid = evt.uid
            print(f"Message from {client_ip} had uid {message_uid}")
            for l in self.allowed_connections:
                #print(l)
                if l[0] != message_uid:
                    continue
                print(f"Found that uid {l[0]} is limited")
                if client_ip in l[1]:
                    print(f"{client_ip} is allowed to send under {message_uid}")
                else:
                    print(f"{client_ip} is not allowed to send under {message_uid}")
                    print()
                    return
            #print(f"Checking if ip {client_ip} is allowed to send under uid {message_uid}")
        except:
            pass
        
        try:
            message_uid = evt.uid
            if message_uid[:7] == "bridge-":
                pass
        except:
            pass
        
        print()

        # If configured, constrain events to a max TTL
        if self.max_ttl >= 0:
            if evt.persist_ttl > self.max_ttl:
                evt.stale = dt.utcnow() + timedelta(seconds=self.max_ttl)

        # Special handling for chat messages
        if isinstance(evt.detail, models.GeoChat):
            chat = evt.detail
            if chat.broadcast:
                self.broadcast(src, evt)
            elif chat.dst_team:
                self.group_broadcast(src, evt, group=chat.dst_team)
            else:
                self.send_user(src, evt, dst_uid=chat.dst_uid)
            return

        # Check for Marti, use first
        if evt.detail and evt.detail.has_marti:
            self.lgr.debug("Handling marti")
            for callsign in evt.detail.marti_cs:
                self.send_user(src, evt, dst_cs=callsign)
            return

        # Assume broadcast
        self.broadcast(src, evt)
