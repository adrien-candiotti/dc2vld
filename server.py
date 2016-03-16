import os
import json

# FIXME : not sure why but docker cloud only displays
#         stderr in its logs ..
import sys
import logging

import etcd
import dockercloud
from dockercloud.api.events import Events

logging.basicConfig(level=logging.DEBUG)

dockercloud.user = os.environ.get('DOCKERCLOUD_USER');
dockercloud.apikey = os.environ.get('DOCKERCLOUD_APIKEY');

if not dockercloud.user or not dockercloud.apikey:
    raise Exception('DOCKERCLOUD_USER && DOCKERCLOUD_APIKEY environment variables must be specified')

infra_stack = os.environ.get('STACK_ENV')

if not infra_stack:
    infra_stack = 'infra'

etcd_hostname = 'etcd.' + infra_stack
etcd_client = etcd.Client(host=etcd_hostname) # FIXME : protocol https?

event_manager = Events()

events = []

containers = {}

def on_open():
    logging.warning('Connection inited with docker cloud api')

def on_close():
    logging.warning('Shutting down')

def get_container(message):
    uri = message.get('resource_uri').split('/')[-2]
    return dockercloud.Container.fetch(uri)

def get_envvar(container, to_find):
    for envvar in container.container_envvars:
        if envvar['key'] == to_find:
            return envvar['value']
    return None

def get_container_hostname(container):
    hostname = container.name
    stack = get_envvar(container, 'DOCKERCLOUD_STACK_NAME')
    if stack:
       hostname = '%s.%s' % (hostname, stack)
    return hostname

def create_backend(backend_name):
    key = '/vulcand/backends/%s/backend' % backend_name
    try:
        etcd_client.read(key)
        return True
    except etcd.EtcdKeyNotFound:
        value = '{"Type": "http"}' # FIXME : https
        etcd_client.write(key, value)
        logging.warning('Created backend : %s' % key)
        return False

def add_https_redirect(backend_name):
    key = '/vulcand/frontends/%s/middlewares/http2https' % backend_name
    try:
        etcd_client.read(key)
        return True
    except etcd.EtcdKeyNotFound:
        value = '{"Type": "rewrite", "Middleware":{"Regexp": "^http://(.*)$", "Replacement": "https://$1", "Redirect": true}}'
        etcd_client.write(key, value)
        logging.warning('Added https redirect middleware : %s' % key)
        return False

def create_frontend(backend_name, ROUTE):
    key = '/vulcand/frontends/%s/frontend' % backend_name
    try:
        etcd_client.read(key)
        return True
    except etcd.EtcdKeyNotFound:
        # NOTE : Route could be passed as a raw string.
        #        More flexible but not needed
        value = '{"Type": "http", "BackendId": "%s", "Route": "PathRegexp(`%s.*`)"}'\
                % (backend_name, ROUTE) # FIXME : https
        etcd_client.write(key, value)
        logging.warning('Created frontend : %s' % key)
        return False

def add_container(container):
    server_name = container.name

    ROUTE = get_envvar(container, 'ROUTE')

    if not ROUTE:
        logging.warning('No route found for container: ' + server_name)
        return

    backend_name = server_name.split('-')[0]
    create_backend(backend_name)

    HOSTNAME = get_container_hostname(container)
    PORT = get_envvar(container, 'PORT')
    ROUTE = get_envvar(container, 'ROUTE')

    if PORT:
        key = '/vulcand/backends/%s/servers/%s' % (backend_name, server_name)
        value = '{"URL": "http://%s:%s"}' % (HOSTNAME, PORT)

        etcd_client.write(key, value)
        logging.warning('Added server: %s = %s on route %s' % (key, value, ROUTE))
        create_frontend(backend_name, ROUTE)
        add_https_redirect(backend_name)
    else:
        logging.warning('No port could be found for this container' + container_name)

def remove_container(container):
    server_name = container.name
    backend_name = server_name.split('-')[0]

    key = '/vulcand/backends/%s/servers/%s' % (backend_name, server_name)
    try:
        etcd_client.delete(key)
        logging.warning('Removed server: %s' % key)
    except etcd.EtcdKeyNotFound as e:
        logging.error(e)

def on_message(message):
    message = json.loads(message)

    if 'type' in message:
        if message['type'] == 'container':
            if 'action' in message:
                if message['action'] == 'update':
                    if message['state'] == 'Running':
                        logging.warning('Running')
                        container = get_container(message)
                        add_container(container)

                    elif message['state'] == 'Stopped': 
                        logging.warning('Stopped')
                        container = get_container(message)
                        remove_container(container)

                elif message['action'] == 'delete':
                      if message['state'] == 'Terminated':
                        logging.warning('Terminated')
                        container = get_container(message)
                        remove_container(container)

def on_error(error):
    logging.error(error)

def create_listener(name, protocol, address):
    key = '/vulcand/listeners/%s' % name
    try:
        etcd_client.read(key)
    except etcd.EtcdKeyNotFound:
        value = '{"Protocol":"%s", "Address":{"Network":"tcp", "Address":"%s"}}' % (protocol, address)
        etcd_client.write(key, value)

event_manager.on_open(on_open)
event_manager.on_close(on_close)
event_manager.on_error(on_error)
event_manager.on_message(on_message)

# FIXME : needed?
create_listener('http', 'http', "0.0.0.0:80")
#create_listener('https', 'https', "0.0.0.0:443")
#create_listener('ws', 'ws', "0.0.0.0:8000") # FIXME websockets, wss

event_manager.run_forever()
