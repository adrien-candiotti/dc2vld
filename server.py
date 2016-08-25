import os
import json

# XXX: not sure why but docker cloud only displays
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

infra_stack = os.environ.get('INFRA_STACK') or 'infra'

# if the ENV environment variable is specified dc2vld will only act when containers
# are part of a specific stack
# example:
#   develop
targeted_stack = os.environ.get('STACK');

etcd_hostname = os.environ.get('ETCD_HOST') or 'etcd.' + infra_stack
etcd_client = etcd.Client(host=etcd_hostname)

event_manager = Events()

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

# -------------------------------------------------------------------------

def insert(key, value, message):
    try:
        etcd_client.read(key)
        logging.warning(key + ' already exists')
        return True
    except etcd.EtcdKeyNotFound:
        etcd_client.write(key, value)
        logging.warning(message)
        return False

def remove(key, message):
    try:
      etcd_client.delete(key)
      logging.warning(message)
      return True
    except etcd.EtcdKeyNotFound as e:
      logging.error(e)
      return False

# -------------------------------------------------------------------------

def create_backend(backend_name):
    key = '/vulcand/backends/%s/backend' % backend_name
    value = '{"Type": "http"}'

    return insert(key, value, 'Created backend : %s' % key)

def create_frontend(backend_name, VERSION, ROUTE):
    key = '/vulcand/frontends/%s/%s/frontend' % ('v' + VERSION, backend_name)
    value = '{"Type": "http", "BackendId": "%s", "Route": "PathRegexp(`/v%s%s.*`)"}'\
            % (backend_name, VERSION, ROUTE)

    return insert(key, value, 'Created frontend : %s' % key)

def create_server(container, backend_name, server_name, VERSION, ROUTE, PORT):
    HOSTNAME = get_container_hostname(container)

    route = "/v" + VERSION + ROUTE

    key = '/vulcand/backends/%s/servers/%s' % (backend_name, server_name)
    value = '{"URL": "http://%s:%s"}' % (HOSTNAME, PORT)

    # Once the service frontend was created we can change the servers as we want
    # That's why we don't use insert,
    # Change the server is essential to be able to change versions
    # or simply relaunch a container that failed
    etcd_client.write(key, value)
    logging.warning('Added server: %s = %s on route %s' % (key, value, route))

# -------------------------------------------------------------------------

def add_https_redirect(backend_name):
    key = '/vulcand/frontends/%s/middlewares/http2https' % backend_name
    value = '{"Type": "rewrite", "Middleware":{"Regexp": "^http://(.*)$", "Replacement": "https://$1", "Redirect": true}}'

    return insert(key, value, 'Added https redirect middleware : %s' % key)

def add_rate_limiting(backend_name):
    key = '/vulcand/frontends/%s/middlewares/rate' % backend_name
    value = '{"Type": "ratelimit", "Middleware":{"Requests": 100, "PeriodSeconds": 1, "Burst": 3, "Variable": "client.ip"}}'

    return insert(key, value, 'Added rate limiting middleware : %s' % key)

# -------------------------------------------------------------------------

def remove_frontend(backend_name):
    key = '/vulcand/frontends/%s/frontend' % backend_name
    remove(key, 'remove frontend : %s' % backend_name)

def add_container(container):
    server_name = container.name
    backend_name = server_name.split('-')[0]

    ROUTE = get_envvar(container, 'ROUTE')
    PORT = get_envvar(container, 'PORT')
    VERSION = get_envvar(container, 'VERSION')

    if targeted_stack:
      STACK = get_envvar(container, 'DOCKERCLOUD_STACK_NAME')
      if STACK != targeted_stack:
        logging.warning('Container is not in targeted stack : %s (%s)' % (server_name, STACK))
        return

    if not ROUTE:
        logging.warning('No route found for this container: ' + server_name)
        return

    if not PORT:
        logging.warning('No port found for this container' + container_name)
        return

    if not VERSION:
        logging.warning('No version found for this container' + container_name)
        return

    # FIXME : If no backend for this key, create backend
    create_backend(backend_name)

    create_server(container, backend_name, server_name, VERSION, ROUTE, PORT)

    # FIXME : If no backend for this key, create frontend
    create_frontend(backend_name, VERSION, ROUTE)

    if os.environ.get('RATE_LIMITING') != None and os.environ.get('RATE_LIMITING') == 'true':
      print 'RATE_LIMITING ON'
      add_rate_limiting(backend_name)

    if os.environ.get('HTTPS') != None and os.environ.get('HTTPS') == 'true':
      print 'HTTPS ON'
      add_https_redirect(backend_name)

def remove_container(container):
    server_name = container.name
    backend_name = server_name.split('-')[0]

    key = '/vulcand/backends/%s/servers/%s' % (backend_name, server_name)

    if targeted_stack:
      STACK = get_envvar(container, 'DOCKERCLOUD_STACK_NAME')
      if STACK != targeted_stack:
        logging.warning('Container is not in targeted stack : %s (%s)' % (server_name, STACK))
        return

    # Same thing here, we only remove the server, not the frontend or the backend
    remove(key, 'Removed server: %s' % key)

    # FIXME : If no server anymore, remove the frontend and backend

# -------------------------------------------------------------------------

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
    value = '{"Protocol":"%s", "Address":{"Network":"tcp", "Address":"%s"}}' % (protocol, address)

    return insert(key, value, 'Added a listener: %s on %s' % (name, address))

event_manager.on_open(on_open)
event_manager.on_close(on_close)
event_manager.on_error(on_error)
event_manager.on_message(on_message)

create_listener('http', 'http', "0.0.0.0:80")

# FIXME : https socket.io?
#create_listener('ws', 'ws', "0.0.0.0:8000")

event_manager.run_forever()
