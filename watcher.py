#!/usr/bin/env python3
import ipaddress
import json
import multiprocessing
import re
import selectors
import socket
import threading
import sys
from util import Node, LogCollector, LogWatchTracker, ClientTracker
from syslog_rfc5424_parser.constants import SyslogSeverity, SyslogFacility


class LogWatchManager:
    def __init__(self):
        self.logWatchTrackers = []
        self.logWatchTrackersLock = threading.Lock()
        self.logSources = {}
        self.logSourcesLock = threading.Lock()
        self.clientTrackers = {}
        self.clientTrackersLock = threading.Lock()
        self.hostAddress = "localhost"
        self.udpPort = 514
        self.tcpPort = 2470
        self.selector = selectors.DefaultSelector()

    def start(self):
        self.startLogCollector()
        self.startServer()
        self.run()

    def run(self):
        while True:
            events = self.selector.select()
            for key, _ in events:
                # collectorPipe
                if key.data == 0:
                    addr, payload = key.fileobj.recv()
                    with self.logSourcesLock:
                        if addr in self.logSources:
                            for lw in self.logSources[addr]:
                                lw.pipe.send(addr, payload)
                # serverPipe: New client is connected.
                elif key.data == 1:
                    sock, addr = key.fileobj.accept()
                    thread = threading.Thread(target=self.clientHandler, args=addr)
                    with self.clientTrackersLock:
                        self.clientTrackers[addr] = ClientTracker(thread, sock)
                    thread.start()
                # LogWatch: A message has come from a LogWatch object
                else:
                    with key.data.lwLock:
                        log = key.fileobj.recv()
                        key.data.logs.append(log)
                    self.notify(key.data, log)

    def register(self, client, lwId):
        try:
            with self.logWatchTrackersLock:
                if lwId >= len(self.logWatchTrackers) or not self.logWatchTrackers[lwId]:
                    return "LogWatch {} does not exists.".format(lwId)
                lw = self.logWatchTrackers[lwId]
            with lw.lwLock:
                if client not in lw.registeredClients:
                    lw.registeredClients.append(client)
                    return "Registered to LogWatch {}".format(lwId)
                else:
                    return "Already registered to LogWatch {}".format(lwId)
        except Exception as e:
            return str(e)

    def unregister(self, client, lwId):
        try:
            with self.logWatchTrackersLock:
                if lwId >= len(self.logWatchTrackers) or not self.logWatchTrackers[lwId]:
                    return "LogWatch {} does not exists.".format(lwId)
                lw = self.logWatchTrackers[lwId]
            with lw.lwLock:
                if client in lw.registeredClients:
                    lw.registeredClients.remove(client)
                    return "Unregistered from LogWatch {}".format(lwId)
                else:
                    return "Already not registered to LogWatch {}".format(lwId)
        except Exception as e:
            return str(e)

    def notify(self, lw, log):
        with self.clientTrackersLock:
            for client in self.clientTrackers:
                if lw in client.registeredWatchers:
                    with client.clientLock:
                        client.write(log)

    def clientHandler(self, addr):
        def managerCreate():
            with self.logWatchTrackersLock:
                lwId = len(self.logWatchTrackers)
                parent_conn, child_conn = multiprocessing.Pipe()
                process = LogWatch(lwId, child_conn)
                lwTracker = LogWatchTracker(process, parent_conn)
                self.logWatchTrackers.append(lwTracker)
                self.selector.register(parent_conn, selectors.EVENT_READ, lwTracker)
            process.start()
            with tracker.clientLock:
                tracker.write("respond\n" + "Created Log Watch {}".format(lwId))

        def managerList():
            with self.logWatchTrackersLock:
                ret = "\n".join(["+" if tracker in lw.registeredClients else " " + str(len(lw.logs)) for lw in self.logWatchTrackers if lw is not None])
            with tracker.clientLock:
                if ret:
                    tracker.write("respond\n" + ret)
                else:
                    tracker.write("respond\n" + "-")

        def managerRegister():
            try:
                ret = self.register(tracker, int(data[1]))
            except Exception as e:
                ret = str(e)
            with tracker.clientLock:
                tracker.write("respond\n" + ret)

        def managerUnregister():
            try:
                ret = self.unregister(tracker, int(data[1]))
            except Exception as e:
                ret = str(e)
            with tracker.clientLock:
                tracker.write("respond\n" + ret)

        def lwSetMatch():
            try:
                lwId = int(data[1])
                ret = " "
                with self.logWatchTrackersLock:
                    if lwId >= len(self.logWatchTrackers) or not self.logWatchTrackers[lwId]:
                        ret = "LogWatch {} does not exists.".format(lwId)
                        lw = None
                    else:
                        lw = self.logWatchTrackers[lwId]
                if lw:
                    args = re.search("(\(.*\)) (\(.*\))", data[2:])
                    if not args:
                        ret = "Invalid Command"
                    else:
                        match = args[0]
                        address = tuple(map(int, args[1]))
                        with lw.lwLock:
                            lw.pipe.write(("setMatch", (match, address)))
                            ret = "Request is sent"
            except Exception as e:
                ret = str(e)
            tracker.write("respond\n" + ret)

        def lwCombineMatch():
            try:
                lwId = int(data[1])
                ret = " "
                with self.logWatchTrackersLock:
                    if lwId >= len(self.logWatchTrackers) or not self.logWatchTrackers[lwId]:
                        ret = "LogWatch {} does not exists.".format(lwId)
                        lw = None
                    else:
                        lw = self.logWatchTrackers[lwId]
                if lw:
                    args = re.search("(\(.*\)) (AND|OR) (\(.*\))", data[2:])
                    if not args:
                        ret = "Invalid Command"
                    else:
                        match = args[0]
                        connector = args[1]
                        address = tuple(map(int, args[1]))
                        with lw.lwLock:
                            lw.pipe.write(("setMatch", (match, connector, address)))
                            ret = "Request is sent"
            except Exception as e:
                ret = str(e)
            tracker.write("respond\n" + ret)

        def lwDelMatch():
            try:
                lwId = int(data[1])
                ret = " "
                with self.logWatchTrackersLock:
                    if lwId >= len(self.logWatchTrackers) or not self.logWatchTrackers[lwId]:
                        ret = "LogWatch {} does not exists.".format(lwId)
                        lw = None
                    else:
                        lw = self.logWatchTrackers[lwId]
                if lw:
                    args = re.search("(\(.*\))", data[2:])
                    if not args:
                        ret = "Invalid Command"
                    else:
                        address = tuple(map(int, args[0]))
                        with lw.lwLock:
                            lw.pipe.write(("setMatch", (address, )))
                            ret = "Request is sent"
            except Exception as e:
                ret = str(e)
            tracker.write("respond\n" + ret)

        def lwSave():
            try:
                lwId = int(data[1])
                ret = " "
                with self.logWatchTrackersLock:
                    if lwId >= len(self.logWatchTrackers) or not self.logWatchTrackers[lwId]:
                        ret = "LogWatch {} does not exists.".format(lwId)
                        lw = None
                    else:
                        lw = self.logWatchTrackers[lwId]
                if lw:
                    with lw.lwLock:
                        lw.pipe.write(("save", ))
                        ret = "Request is sent"
            except Exception as e:
                ret = str(e)
            tracker.write("respond\n" + ret)

        def lwLoad():
            try:
                lwId = int(data[1])
                ret = " "
                with self.logWatchTrackersLock:
                    if lwId >= len(self.logWatchTrackers) or not self.logWatchTrackers[lwId]:
                        ret = "LogWatch {} does not exists.".format(lwId)
                        lw = None
                    else:
                        lw = self.logWatchTrackers[lwId]
                if lw:
                    with lw.lwLock:
                        lw.pipe.write(("load", ))
                        ret = "Request is sent"
            except Exception as e:
                ret = str(e)
            tracker.write("respond\n" + ret)

        tracker = self.clientTrackers[addr]
        managerMethods = {"create": managerCreate, "list": managerList, "register": managerRegister,
                          "unregister": managerUnregister}
        lwMethods = {"setMatch": lwSetMatch, "combineMatch": lwCombineMatch, "delMatch": lwDelMatch, "save": lwSave,
                     "load": lwLoad}
        while True:
            data = tracker.read()
            data = data.split(' ')

            # LogWatchManager Method
            if data[0] in managerMethods:
                managerMethods[data[0]]()
            elif data[0] in lwMethods:
                lwMethods[data[0]]()
            elif data[0] == "select":
                tracker.selectedWatcher = int(data[1])
                tracker.write("Success")

            if tracker.selectedWatcher is None:
                tracker.write()

    def startLogCollector(self):
        collectorPipe, externalPipe = multiprocessing.Pipe()
        collector = LogCollector(self.hostAddress, self.udpPort, externalPipe)
        collector.start()
        self.selector.register(collectorPipe, selectors.EVENT_READ, 0)

    def startServer(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind((self.hostAddress, self.tcpPort))
        sock.listen(10)
        self.selector.register(sock, selectors.EVENT_READ, 1)


class LogWatch(multiprocessing.Process):
    """LogWatch class for watching log sources
    match -> (matchfield, operator, value, negated, caseinsens)
    matchfield -> one of (WHOLE, IP, SEVERITY, FACILITY, FIELD:range:sep, RE:regexp:field)
    """

    def __init__(self, lwId, pipe):
        super(LogWatch, self).__init__()
        self.pipe = pipe
        self.rules = Node()
        self.lwId = lwId

    def run(self):
        data = self.pipe.recv()
        while data:
            if data[0] == "setMatch":
                self.setMatch(*data[1:])
            elif data[0] == "combineMatch":
                self.combineMatch(*data[1:])
            elif data[0] == "delMatch":
                self.delMatch(*data[1:])
            elif data[0] == "save":
                self.save()
            elif data[0] == "load":
                self.load()
            elif data[0] == "log":
                if self.applyFilters(self.rules, data[1].as_dict()):
                    self.pipe.send(str(data[1]))
            else:
                pass
            data = self.pipe.recv()
        self.pipe.close()

    def applyFilters(self, rules, payload):
        if rules.value == "AND":
            return self.applyFilters(rules.left, payload) and self.applyFilters(rules.right, payload)
        elif rules.value == "OR":
            return self.applyFilters(rules.left, payload) or self.applyFilters(rules.right, payload)
        else:
            return self.applyRule(rules.value, payload)

    def applyRule(self, rule, payload):
        class InvalidMatchfield(Exception):
            pass

        class InvalidOperator(Exception):
            pass

        def applyMatch(operand):
            arg1 = value
            arg2 = operand

            if caseinsens and type(operand) == str:
                arg1 = arg1.lower()
                arg2 = arg2.lower()
            if operator == "EQ":
                ret = arg1 == arg2
            elif operator == "LT":
                ret = arg1 < arg2
            elif operator == "LE":
                ret = arg1 <= arg2
            elif operator == "GT":
                ret = arg1 > arg2
            elif operator == "GE":
                ret = arg1 >= arg2
            elif operator == "RE":
                ret = re.match(arg1, arg2) is not None
            else:
                raise InvalidOperator("Invalid operator {0} in rule {1}".format(operator, rule))
            if not negated:
                return ret
            else:
                return not ret

        matchfield = rule[0]
        operator = rule[1]
        value = rule[2]
        negated = rule[3]
        caseinsens = rule[4]

        if matchfield == "WHOLE":
            return applyMatch(payload["msg"])

        elif matchfield == "IP":
            if re.match('\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}', value) and re.match('\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}',
                                                                                  payload["hostname"]):
                value = ipaddress.IPv4Address(value)
                return applyMatch(ipaddress.IPv4Address(payload["hostname"]))
            elif type(value) == str and type(payload["hostname"] == str):
                return applyMatch(payload["hostname"])
            else:
                return False

        # emerg is the highest, debug is the lowest severity
        elif matchfield == "SEVERITY":
            if type(value) == str:
                value = 7 - SyslogSeverity[value.lower()]
            else:
                value = 7 - value
            severity = 7 - SyslogSeverity[payload["severity"]]
            return applyMatch(severity)

        # kern is the highest(0), unknown is the lowest (-1), if unknown is not present local7 is the lowest (23)
        elif matchfield == "FACILITY":
            if type(value) == str:
                value = 23 - SyslogFacility[value.lower()]
            else:
                value = 23 - value
            facility = 23 - SyslogFacility[payload["facility"]]
            if facility == 24:
                facility = -1
            if value == 24:
                value = -1
            return applyMatch(facility)

        elif matchfield.startswith("FIELD:"):
            fieldSplitList = matchfield.split(':')
            fieldStartRange = int(fieldSplitList[1][0])
            delimiter = fieldSplitList[2]
            if len(fieldSplitList[1]) != 1:
                fieldEndRange = int(fieldSplitList[1][2:]) + 1
                return applyMatch(delimiter.join(payload["msg"].split(delimiter)[fieldStartRange:fieldEndRange]))
            else:
                return applyMatch("".join(payload["msg"].split(delimiter)[fieldStartRange]))

        elif matchfield.startswith("RE:"):
            regexSplitList = matchfield.split(':')
            regexp = regexSplitList[1]
            field = regexSplitList[2]
            return applyMatch(re.sub(regexp, '\g<' + field + '>', payload["msg"]))

        else:
            raise InvalidMatchfield("Invalid matchfield {0} in rule {1}".format(matchfield, rule))

    # Set addressed Node to given "match" value.
    def setMatch(self, match, address=()):
        self.rules.getNode(address).value = match

    # Set the the addressed node to given "connector" value. ("AND" or "OR")
    # Left branch of connector will be the previous node's match value, right branch will be the new match value.
    def combineMatch(self, match, connector, address=()):
        node = self.rules.getNode(address)
        temp = node.value
        node.value = connector
        node.left = Node(temp)
        node.right = Node(match)

    # Delete the node at given address, the sibling of the node will replace the parent logical operator.
    def delMatch(self, address=()):
        # Deleting the rules
        if address == ():
            if self.rules.left is None and self.rules.right is None:
                self.rules.value = None
                self.rules.left = None
                self.rules.right = None
        else:
            parentNode = self.rules.getNode(address[:-1])
            if address[-1] == 0:
                survivorNode = parentNode.right
            elif address[-1] == 1:
                survivorNode = parentNode.left
            else:
                print("Invalid address:", address, file=sys.stderr)
                return
            parentNode.value = survivorNode.value
            parentNode.left = survivorNode.left
            parentNode.right = survivorNode.right

    # Save current configuration as JSON to a file
    # Configuration -> log source path + rule tree
    def save(self):
        with open("LogWatch{}.json".format(str(self.lwId)), "w") as writeFile:
            json.dump(self.rules, writeFile, indent=4)

    # Load configuration from JSON file
    def load(self):
        with open("LogWatch{}.json".format(str(self.lwId)), "r") as readFile:
            data = json.load(readFile)
        self.rules.load(data)
