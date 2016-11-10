from twisted.internet.protocol import DatagramProtocol
from twisted.internet import reactor, protocol, task, defer, threads
import petlib.pack
from petlib.ec import EcGroup, EcPt
from petlib.bn import Bn
from twisted.internet import reactor, defer
import format3
from mixnode import MixNode
import base64
import random
import threading
import codecs
import time
import databaseConnect as dc
import sys
import sqlite3
import io
import csv
from twisted.internet.defer import DeferredQueue
import supportFunctions as sf
from processQueue import ProcessQueue
import numpy


# from twisted.logger import jsonFileLogObserver, Logger
# log = Logger(observer=jsonFileLogObserver(io.open("log.json", "a")))

class Provider(MixNode):

    def __init__(self, name, port, host, setup, privk=None, pubk=None):
        """ A class representing a provider. """
        MixNode.__init__(self, name, port, host, setup, privk, pubk)

        self.clientList = {}
        self.usersPubs = []
        self.storage = {}
        self.replyBlocks = {}
        self.MAX_RETRIEVE = 500
        # self.Queue = []

        self.bSent = 0
        self.bReceived = 0
        self.bProcessed = 0
        self.gbSent = 0
        self.gbReceived = 0

        self.receivedQueue = DeferredQueue()

        self.nMsgSent = 0
        self.testReceived = 0

        self.testQueueSize = 0

        self.processQueue = ProcessQueue()

        self.bProcList = []
        self.gbRecList = []
        self.bRecList = []
        self.hbSentList = []
        self.hbSent = 0
        self.hbRecList = []
        self.hbRec = 0

    def startProtocol(self):
        reactor.suggestThreadPoolSize(30)

        print "[%s] > Start protocol." % self.name
        
        reactor.callLater(10.0, self.turnOnProcessing)

        # self.run()
        self.d.addCallback(self.turnOnHeartbeats)
        self.d.addErrback(self.errbackHeartbeats)

        self.turnOnReliableUDP()
        self.readInData('example.db')

        self.turnOnMeasurments()
        self.saveMeasurments()

    def stopProtocol(self):
        print "[%s] > Stop protocol" % self.name

    def turnOnProcessing(self):
        #self.receivedQueue.get().addCallback(self.do_PROCESS)
        self.processQueue.get().addCallback(self.do_PROCESS)

    def datagramReceived(self, data, (host, port)):
        #self.receivedQueue.put((data, (host, port)))
        self.bReceived += 1
        try:
            self.processQueue.put((data, (host, port)))
        except Exception, e:
            print "[%s] > ERROR: %s " % (self.name, str(e))

    def do_PROCESS(self, obj):
        data, (host, port) = obj
        #self.receivedQueue.get().addCallback(self.do_PROCESS)

        self.processMessage(obj)

        try:
            #reactor.callFromThread(self.processQueue.get().addCallback, self.do_PROCESS)
            reactor.callFromThread(self.get_and_addCallback, self.do_PROCESS)
        except Exception, e:
            print "[%s] > ERROR: %s" % (self.name, str(e))

    def get_and_addCallback(self, f):
        self.processQueue.get().addCallback(f)

    def processMessage(self, obj):
        data, (host, port) = obj
        self.bProcessed += 1

        if data[:8] == "PULL_MSG":
            self.do_PULL(data[8:], (host, port))
        # if data[:4] == "RINF":
        #     dataList = petlib.pack.decode(data[4:])
        #     for element in dataList:
        #         self.mixList.append(
        #             format3.Mix(
        #                 element[0],
        #                 element[1],
        #                 element[2],
        #                 element[3]))
        # if data[:4] == "UPUB":
        #     dataList = petlib.pack.decode(data[4:])
        #     for element in dataList:
        #         self.usersPubs.append(format3.User(element[0], element[1], element[2], element[3], element[4]))
        #     # print "[%s] > Provider received public information of users registered in the system" % self.name
        # if data[:4] == "INFO":
        #     self.sendInfoMixnet(host, port)
        #     # print "[%s] > Provider received request for information from %s, %d " % (self.name, host, port)
        if data[:4] == "ROUT":
            try:
                self.gbReceived += 1
                idt, msgData = petlib.pack.decode(data[4:])
                self.sendMessage("ACKN"+idt, (host, port))
                self.do_ROUT(msgData, (host, port))
            except Exception, e:
                print "[%s] > ERROR: " % self.name, str(e)
        if data[:4] == "ACKN":
            if data in self.expectedACK:
                self.expectedACK.remove(data)
        if data[:4] == "PING":
            self.subscribeClient(data[4:], host, port)

    def do_PULL(self, name, (host, port)):
        """ Function which responds the pull message request from the client. First, the function checks if the requesting 
        client belogs to this provider, next takes all the messages destinated to this client and sends a fixed number of 
        messages to the client.

                Args:
                host (str): host of the requesting client,
                port (int): port of the requesting client.
        """

        def send_to_ip(IPAddrs):

            if name in self.storage.keys():
                if self.storage[name]:
                    for _ in range(self.MAX_RETRIEVE):
                       if self.storage[name]:
                           message = self.storage[name].pop()
                           self.transport.write("PMSG" + message, (IPAddrs, port))
                           print "message poped"
                else:
                    self.transport.write("NOMSG", (IPAddrs, port))

            else:
                self.transport.write("NOASG", (IPAddrs, port))

        reactor.resolve(host).addCallback(send_to_ip)


    def do_ROUT(self, data, (host, port), tag=None):
        """ Function operates on the received route packet. First, the function decrypts one layer on the packet. Next, if 
        it is a forward message adds it to the queue. If it is a message destinated to one of the providers clients, the message 
        is saved in the storage, where it awaits for the client to fetch it.

                Args:
                data (str): traversing packet string,
                host (str): host of the sender of the packet
                port (int): port of the sender of the packet
        """
        # print "[%s] > Received ROUT message from %s, %d " % (self.name, host, port)
        try:
            peeledData = self.mix_operate(self.setup, data)
        except Exception, e:
            print "[%s] > ERROR during packet processing." % self.name
        else:
            if peeledData:
                (xtoPort, xtoHost, xtoName), msg_forw, idt, delay = peeledData
                def save_or_queue(IPAddrs):
                    if xtoName in self.clientList.keys():
                        self.saveInStorage(xtoName, msg_forw)
                    else:
                        # self.addToQueue(
                        #     ("ROUT" + petlib.pack.encode((idt ,msg_forw)), (IPAddrs, xtoPort), idt), delay)
                        #print "[%s] > Decryption ended. Message destinated to (%d, %s) " % (self.name, xtoPort, IPAddrs)
                        try:
                            if delay > 0:
                                reactor.callLater(delay, self.sendMessage, "ROUT" + petlib.pack.encode((idt ,msg_forw)), (IPAddrs, xtoPort))
                            else:
                                self.sendMessage("ROUT" + petlib.pack.encode((idt ,msg_forw)), (IPAddrs, xtoPort))
                            self.expectedACK.add("ACKN"+idt)
                        except Exception, e:
                            print "ERROR during ROUT: ", str(e)
                reactor.resolve(xtoHost).addCallback(save_or_queue)

    def saveInStorage(self, key, value):
        """ Function saves a message in the local storage, where it awaits till the client will fetch it.

                Args:
                key (int): clients key,
                value (str): message (encrypted) stored for the client.
        """
        if key in self.storage.keys():
            self.storage[key].append(petlib.pack.encode((value, time.time())))
        else:
            self.storage[key] = [petlib.pack.encode((value, time.time()))]
        # if key not in self.storage.keys():
        #    self.storage[key] = set(petlib.pack.encode((value, time.time())))
        # else:
        #    self.storage[key].add(petlib.pack.encode((value, time.time())))
        # print "[%s] > Saved message for User %s in storage" % (self.name, key)

    def subscribeClient(self, name, host, port):
        if name not in self.clientList.keys():
            self.clientList[name] = (host, port)
        #else:
        #    self.clientList[name] = (host, port)

    def sendInfoMixnet(self, host, port):
        """ Function forwards the public information about the mixnodes and users in the system to the requesting address.

                Args:
                host (str): requesting host
                port (port): requesting port
        """
        if not self.mixList:
            self.transport.write("EMPT", (host, port))
        else:
            self.transport.write("RINF" + petlib.pack.encode(self.mixList), (host, port))

    def sendInfoUsers(self, host, port):
        if not self.usersPubs:
            self.transport.write("EMPT", (host, port))
        else:
            self.transport.write("UINF" + petlib.pack.encode(self.usersPubs), (host, port))

    def saveInDB(self, databaseName):
        data = [self.name, self.port, self.host, self.pubk]
        db = sqlite3.connect(databaseName)
        c = db.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS %s (id INTEGER PRIMARY KEY, name text, port integer, host text, pubk blob)'''%"Providers")
        insertQuery = "INSERT INTO Providers VALUES(?, ?, ?, ?, ?)"
        c.execute(insertQuery, [None, self.name, str(self.port), self.host, sqlite3.Binary(petlib.pack.encode(self.pubk))])
        db.commit()
        db.close()
        print "[%s] > Provider public information saved in database." % self.name


    def turnOnMeasurments(self):
        lc = task.LoopingCall(self.measurments)
        lc.start(60, False)

    def saveMeasurments(self):
        lc = task.LoopingCall(self.save_to_file)
        lc.start(100, False)

    def measurments(self):
        self.bProcList.append(self.bProcessed)
        self.bProcessed = 0
        self.gbRecList.append(self.gbReceived)
        self.gbReceived = 0
        self.bRecList.append(self.bReceived)
        self.bReceived = 0
        self.hbSentList.append(self.hbSent)
        self.hbSent = 0
        self.hbRecList.append(self.hbRec)
        self.hbRec = 0
        self.pProcList.append(self.pProcessed)
        self.pProcessed = 0
        # try:
        #     with open("performanceProvider.csv", "ab") as outfile:
        #         csvW = csv.writer(outfile, delimiter=',')
        #         data = [[num, good, received]]
        #         csvW.writerows(data)
        # except Exception, e:
        #     print "ERROR - ", str(e)

    def save_to_file(self):
        avg_bProcessed = numpy.mean(self.bProcList)
        std_bProcessed = numpy.std(self.bProcList)
        self.bProcList = []
        avg_gbReceived = numpy.mean(self.gbRecList)
        std_gbReceived = numpy.std(self.gbRecList)
        self.gbRecList = []
        avg_bReceived = numpy.mean(self.bRecList)
        std_bReceived = numpy.std(self.bRecList)
        self.bRecList = []
        hbSent = numpy.mean(self.hbSentList)
        self.hbSentList = []
        hbRec = numpy.mean(self.hbRecList)
        self.hbRecList = []
        avg_pProc = numpy.mean(self.pProcList)
        std_pProc = numpy.std(self.pProcList)
        self.pProcList = []
        try:
            with open("performanceProvider.csv", "ab") as outfile:
                csvW = csv.writer(outfile, delimiter=',')
                data = [[avg_bProcessed, std_bProcessed, avg_gbReceived, std_gbReceived, avg_bReceived, std_bReceived, avg_pProc, std_pProc, hbSent, hbRec]]
                csvW.writerows(data)
        except Exception, e:
            print "ERROR saving to file: ", str(e)



