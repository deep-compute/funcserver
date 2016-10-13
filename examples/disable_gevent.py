from gevent import monkey; monkey.patch_all = lambda: None

from funcserver import Server

class MyServer(Server):
    def run(self):
        # do something here including using
        # `multiprocessing` module
        pass

if __name__ == '__main__':
    MyServer()
