var MSG_TYPE_INFO = 0
var MSG_TYPE_CONSOLE = 1
var MSG_TYPE_LOG = 2

function ServerSocket(pysession) {
    var self = this;
    self.pysession = pysession;

    self.get_ws_link = function() {
        return 'ws://' + window.location.host + '/ws/' + self.pysession;
    }

    // Constructor
    self.ws = new WebSocket(self.get_ws_link());
    self.is_ws_active = false;
    self.msg_counter = 0;

    // Websocket callbacks
    self.on_message = function(msg) {
        msg = JSON.parse(msg.data);
        self.onmessage(msg);
    };

    self.on_open = function() {
        self.is_ws_active = true;
        self.onopen();
    };

    self.on_close = function() {
        self.is_ws_active = false;
        self.onclose();
    };

    self.on_error = function() {
        self.onerror();
    };

    self.send = function(msg, cb) {
        msg.id = self.msg_counter;
        self.msg_counter++;
        self.ws.send(JSON.stringify(msg));
    }

    self.ws.onmessage = function(msg) {self.on_message(msg);};
    self.ws.onopen = function() {self.on_open();};
    self.ws.onclose = function() { self.on_close(); };
    self.ws.onerror = function() { self.on_error(); };

    self.onmessage = function(msg) {};
    self.onopen = function() {};
    self.onclose = function() {};
    self.onerror = function() {};
}
