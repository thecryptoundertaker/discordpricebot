import json
import os
from decimal import Decimal, DecimalException
from urllib.parse import urlparse

import discord
from discord.ext import tasks, commands
from urllib.request import urlopen, Request
from web3 import Web3
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

def fetch_abi(contract, bsc_apikey):
    if not os.path.exists('contracts'):
        os.mkdir('./contracts')

    filename = f'contracts/{contract}.json'
    if os.path.exists(filename):
        with open(filename, 'r') as abi_file:
            abi = abi_file.read()
    else:
        # TODO: Error handling
        url = 'https://api.ftmscan.com/api?module=contract&action=getabi&address=' + contract + '&apikey=' + bsc_apikey
        abi_response = urlopen(Request(url, headers={'User-Agent': 'Mozilla'})).read().decode('utf8')
        abi = json.loads(abi_response)['result']

        with open(filename, 'w') as abi_file:
            abi_file.write(abi)
    return json.loads(abi)

def list_cogs(directory):
    basedir = (os.path.basename(os.path.dirname(__file__)))
    return (f"{basedir}.{directory}.{f.rstrip('.py')}" for f in os.listdir(basedir + '/' + directory) if f.endswith('.py'))

class PriceBot(commands.Bot):
    contracts = {}
    config = {}
    current_price = 0
    nickname = ''
    bnb_amount = 0
    bnb_price = 0
    token_amount = 0
    total_supply = 0
    display_precision = Decimal('0.0001')  # Round to 4 token_decimals

    # Static BSC contract addresses
    address = {
        'bnb' : '0x21be370D5312f44cB42ce377BC9b8a0cEF1A4C83',
        'busd': '0x049d68029688eAbF473097a2fC38ef61633A3C7A'
    }

    decimals = {
        'bnb': 18,
        'busd': 6
    }

    intents = discord.Intents.default()
    intents.members = True

    def __init__(self, config, token):
        super().__init__(command_prefix=self.handle_prefix, case_insensitive=True)
        self.config = config
        self.token = token
        self.amm = config['amm'][token['from']]

        if not config['amm'].get(token['from']):
            raise Exception(f"{token['name']}'s AMM {token['from']} does not exist!")

        if node := config.get('bsc_node'):
            bsc_node = urlparse(node)
            if 'http' in bsc_node.scheme:
                provider = Web3.HTTPProvider(node)
            else:
                provider = Web3.IPCProvider(bsc_node.path)

            self.web3 = Web3(provider)  # type: Web3.eth.account
        else:
            raise Exception("Required setting 'bsc_node' not configured!")

        self.contracts['bnb'] = self.web3.eth.contract(address=self.address['bnb'], abi=self.token['abi'])
        self.contracts['busd'] = self.web3.eth.contract(address=self.address['busd'], abi=self.token['abi'])
        self.contracts['token'] = self.web3.eth.contract(address=self.token['contract'], abi=self.token['abi'])
        self.contracts['lp'] = self.web3.eth.contract(address=self.token['lp'], abi=fetch_abi(self.token['lp'], self.config['bscscan_apikey']))

        if not self.token.get('decimals'):
            self.token['decimals'] = self.contracts['token'].functions.decimals().call()

        self.help_command = commands.DefaultHelpCommand(command_attrs={"hidden": True})

        self.dbengine = create_engine('sqlite:///pricebot.db', echo=True)
        session = sessionmaker(bind=self.dbengine)
        self.db = session()

    def handle_prefix(self, bot, message):
        if isinstance(message.channel, discord.channel.DMChannel):
            return ''

        return commands.when_mentioned(bot, message)

    def get_amm(self, amm=None):
        if not amm:
            return self.amm

        return self.config['amm'].get(amm)

    def icon_value(self, value=None):
        if self.token['emoji'] or self.token['icon']:
            value = f" {value}" if value else ''
            return f"{self.token['emoji'] or self.token['icon']}{value}"

        value = f"{value} " if value else ''
        return f"{value}{self.token['name']}"

    def get_bnb_price(self, lp):
        bnb_amount = Decimal(self.contracts['bnb'].functions.balanceOf(lp).call()) * Decimal(10 ** (18 - self.decimals['bnb']))
        busd_amount = Decimal(self.contracts['busd'].functions.balanceOf(lp).call()) * Decimal(10 ** (18 - self.decimals['busd']))

        self.bnb_price = Decimal(busd_amount) / Decimal(bnb_amount)

        return self.bnb_price

    def get_price(self, token_contract, native_lp, bnb_lp):
        self.bnb_amount = Decimal(self.contracts['bnb'].functions.balanceOf(native_lp).call())
        self.token_amount = Decimal(token_contract.functions.balanceOf(native_lp).call()) * Decimal(10 ** (18 - self.token["decimals"]))  # Normalize token_decimals

        bnb_price = self.get_bnb_price(bnb_lp)


        try:
            final_price = self.bnb_amount / self.token_amount * bnb_price
        except ZeroDivisionError:
            final_price = 0

        return final_price

    def get_token_price(self):
        return self.get_price(self.contracts['token'], self.token['lp'], self.amm['address']).quantize(self.display_precision)

    def generate_presence(self):
        if not self.token_amount:
            return ''

        try:
            total_supply = self.contracts['lp'].functions.totalSupply().call()
            values = [Decimal(self.token_amount / total_supply), Decimal(self.bnb_amount / total_supply)]
            lp_price = self.current_price * values[0] * 2

            return f"LP ≈${round(lp_price, 2)} | {round(values[0], 4)} {self.token['icon']} + {round(values[1], 4)} FTM"
        except ValueError:
            pass

    def generate_nickname(self):
        return f"{self.token['icon']} ${self.current_price:.2f} ({round(self.bnb_amount / self.token_amount, 4):.4f} FTM)"

    async def get_lp_value(self):
        self.total_supply = self.contracts['lp'].functions.totalSupply().call()
        return [self.token_amount / self.total_supply, self.bnb_amount / self.total_supply]

    async def on_guild_join(self, guild):
        await guild.me.edit(nick=self.nickname)

    async def check_restrictions(self, ctx):
        server_restriction = self.config.get('restrict_to', {}).get(ctx.guild.id)
        if server_restriction and not await self.is_owner(ctx.author):
            if ctx.channel.id not in server_restriction:
                if ctx.channel.permissions_for(ctx.guild.me).manage_messages:
                    await ctx.message.delete()
                return False
        return True

    async def on_ready(self):
        restrictions = self.config.get('restrict_to', {})
        all_channels = self.get_all_channels()
        for guild_id, channels in restrictions.items():
            for i, channel in enumerate(channels):
                if not self.parse_int(channel):
                    channels[i] = discord.utils.get(all_channels, guild__id=guild_id, name=channel)
                    if not channels[i]:
                        raise Exception('No channel named channel!')

    @staticmethod
    def parse_int(val):
        try:
            val = int(val)
        except ValueError:
            val = None

        return val

    @staticmethod
    def parse_decimal(val):
        try:
            val = Decimal(val)
        except (TypeError, DecimalException):
            val = None

        return val

    def exec(self):
        for cog in list_cogs('commands'):
            try:
                if self.token.get('command_override'):
                    override = self.token.get('command_override')
                    cog = override.get(cog, cog)

                self.load_extension(cog)
            except Exception as e:
                print(f'Failed to load extension {cog}.', e)

        self.run(self.token['apikey'])
