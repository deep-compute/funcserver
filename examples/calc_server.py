from funcserver import Server

class CalcAPI(object):
    def __init__(self, ignore_divbyzero=False):
        self.ignore_divbyzero = ignore_divbyzero

    def add(self, a, b):
        '''Computes the sum of @a and @b'''
        return a + b

    def sub(self, a, b):
        '''Computes the difference of @a and @b'''
        return a - b

    def mul(self, a, b):
        '''Computes the product of @a and @b'''
        return a * b

    def div(self, a, b):
        '''Computes the division of @a by @b'''
        if self.ignore_divbyzero: return 0
        return a / b

class CalcServer(Server):
    NAME = 'CalcServer'
    DESC = 'Calculation Server'

    def prepare_api(self):
        return CalcAPI(self.args.ignore_divbyzero)

    def define_args(self, parser):
        super(CalcServer, self).define_args(parser)
        parser.add_argument('--ignore-divbyzero', default=False,
            action='store_true',
            help='Ignore division by zero errors')

if __name__ == '__main__':
    CalcServer().start()
