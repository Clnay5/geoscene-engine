"""Bounded LRU with TTL and per-key single flight. Exceptions are never cached."""
from collections import OrderedDict
from threading import Lock, Event
from time import monotonic

class TileCache:
    def __init__(self, capacity=64, ttl=300):
        self.capacity=capacity;self.ttl=ttl;self.lock=Lock();self.entries=OrderedDict();self.flights={}
        self.hits=0;self.misses=0
    def get_or_compute(self,key,compute):
        while True:
            with self.lock:
                entry=self.entries.get(key)
                if entry and monotonic()-entry[0]<self.ttl:
                    self.entries.move_to_end(key);self.hits+=1;return entry[1],True
                if key in self.flights:event=self.flights[key];owner=False
                else:
                    event=Event();self.flights[key]=event;owner=True;self.misses+=1
            if owner:break
            event.wait()
        try:
            value=compute()
            with self.lock:
                self.entries[key]=(monotonic(),value);self.entries.move_to_end(key)
                while len(self.entries)>self.capacity:self.entries.popitem(last=False)
            return value,False
        finally:
            with self.lock:self.flights.pop(key,None);event.set()
    def info(self):
        with self.lock:return {'entries':len(self.entries),'capacity':self.capacity,'ttl_seconds':self.ttl,'hits':self.hits,'misses':self.misses,'in_flight':len(self.flights)}
