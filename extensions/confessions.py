from array import array
import asyncio, re, time
from typing import Optional, Union
import disnake
from disnake.ext import commands

class CHANNEL_TYPE:
	invalid = -2
	none = -1
	untraceable = 0
	traceable = 1
	vetting = 2
	feedback = 3

class PendingConfession:
	def __init__(self, input:Optional[str]=None, *, vetmessage:disnake.Message=None, choicemsg:disnake.Message=None, targetchannel:disnake.TextChannel=None, content:str=None, image:Optional[str]=None):
		if input:
			self.offline = True
			self.vetmessage = vetmessage
			lines = input.splitlines()
			self.choicechannel_id = int(lines[0])
			self.choicemsg_id = int(lines[1])
			self.targetchannel_id = int(lines[2])
			self.image = lines[3] if len(lines[3]) else None
			self.failures = int(lines[4])
			self.content = '\n'.join(lines[5:])
		elif input == '':
			pass
		else:
			self.offline = False
			self.choicechannel = choicemsg.channel
			self.choicemsg = choicemsg
			self.targetchannel = targetchannel
			self.content = content
			self.image = image if image else None
			self.failures = 0
			self.vetmessage = vetmessage
	
	def __str__(self) -> str:
		if self.offline:
			return \
				str(self.choicechannel_id) + '\n' + \
				str(self.choicemsg_id) + '\n' + \
				str(self.targetchannel_id) + '\n' + \
				(str(self.image) if self.image else '') + '\n' + \
				str(self.failures) + '\n' + \
				str(self.content)
		else:
			return \
				str(self.choicechannel.id) + '\n' + \
				str(self.choicemsg.id) + '\n' + \
				str(self.targetchannel.id) + '\n' + \
				(str(self.image) if self.image else '') + '\n' + \
				str(self.failures) + '\n' + \
				str(self.content)
	
	async def fetch(self, bot:commands.Bot):
		if self.offline:
			self.choicechannel = await bot.fetch_channel(self.choicechannel_id)
			self.choicemsg = await self.choicechannel.fetch_message(self.choicemsg_id)
			self.targetchannel = await bot.fetch_channel(self.targetchannel_id)
			self.offline = False

			
class Confessions(commands.Cog):
	"""Note that commands in this module have generic names which may clash with other commands
	or not make any sense outside of a confessions bot."""

	channel_icons = {CHANNEL_TYPE.untraceable: '🙈', CHANNEL_TYPE.traceable: '👁', CHANNEL_TYPE.feedback: '📢'}

	def __init__(self, bot:commands.Bot):
		self.bot = bot
		if not bot.config.getboolean('extensions', 'auth', fallback=False):
			raise Exception("'auth' must be enabled to use 'confessions'")
		# ensure config file has required data
		if not bot.config.has_section('confessions'):
			bot.config.add_section('confessions')
		if 'starttime' not in bot.config['confessions']:
			bot.config['confessions']['starttime'] = '?'
		if 'confession_cooldown' not in bot.config['confessions']:
			bot.config['confessions']['confession_cooldown'] = 1
		if 'anonid_generator' not in bot.config['confessions']:
			bot.config['confessions']['anonid_generator'] = '#import\nanonid = hex(uuid)[-6:]'
			print("WARNING: you should define a more advanced algorithm for hiding user ids. (config[confessions][anonid_generator])")
		self.initiated = set()
		self.ignore = set()
		self.confession_cooldown = dict()

	"""
		Utility functions
	"""

	def get_anonid(self, guildid:int, userid:int):
		offset = self.bot.config.getint('confessions', str(guildid)+'_shuffle', fallback=0)
		loc = {'uuid' : guildid+userid+offset, 'anonid' : None}
		exec(self.bot.config['confessions']['anonid_generator'], None, loc)
		return loc['anonid']

	def generate_list(self, user:disnake.User, matches:array, vetting:bool, enum:bool=False):
		return ',\n'.join([(str(i+1)+':' if enum else '') + f'{self.channel_icons[c[1]]}<#{c[0].id}>'+(' ('+c[0].guild.name+')' if not isinstance(user, disnake.Member) else '') for i,c in enumerate(matches)]) +\
						('\n'+self.bot.babel(user, 'confessions', 'vetting') if vetting else '')

	def scanguild(self, member:disnake.Member):
		matches = []
		vetting = False
		save = False
		for channel in member.guild.channels:
			# Backwards compatibility (v1.x): add server id to confession channel config
			if '?_'+str(channel.id) in self.bot.config['confessions']:
				save = True
				self.bot.config['confessions'][str(member.guild.id)+'_'+str(channel.id)] = self.bot.config['confessions']['?_'+str(channel.id)]
				self.bot.config.remove_option('confessions', '?_'+str(channel.id))
			if str(member.guild.id)+'_'+str(channel.id) in self.bot.config['confessions']:
				chtype = int(self.bot.config['confessions'][str(member.guild.id)+'_'+str(channel.id)])
				if chtype == CHANNEL_TYPE.vetting:
					vetting = True
					continue
				if chtype == CHANNEL_TYPE.feedback:
					matches.append((channel, chtype))
					continue
				if channel.permissions_for(member).read_messages:
					matches.append((channel, chtype))
					continue
		
		if save:
			self.bot.config.save()
		
		return matches, vetting

	def listavailablechannels(self, user:Union[disnake.User, disnake.Member]):
		if isinstance(user, disnake.Member):
			matches,vetting = self.scanguild(user)
		else:
			matches = []
			vetting = False
			for guild in self.bot.guilds:
				if member := guild.get_member(user.id):
					newmatches, newvetting = self.scanguild(member)
					matches += newmatches
					vetting = vetting or newvetting
		
		return matches, vetting

	def generate_confession(self, anonid:str, lead:str, content:str, image:Optional[str]):
		if lead:
			embed = disnake.Embed(colour=disnake.Colour(int(anonid,16)),description=lead+' '+content)
		else:
			embed = disnake.Embed(description=content)
		if image:
			embed.set_image(url=image)
		return embed

	async def send_confession(self, ctx:Union[disnake.DMChannel, disnake.Interaction], targetchannel:disnake.TextChannel, embed:disnake.Embed):
		""" Sends confessions through, plus the copious amounts of error handling """
		try:
			await targetchannel.send(embed=embed)
		except disnake.errors.Forbidden:
			try:
				await targetchannel.send(self.bot.babel(targetchannel.guild, 'confessions', 'missingperms', perm='Embed Links'))
				if isinstance(ctx, disnake.DMChannel):
					await ctx.send(self.bot.babel(ctx, 'confessions', 'embederr'))
				else:
					await ctx.response.send_message(self.bot.babel(ctx, 'confessions', 'embederr'), ephemeral=True)
			except:
				if isinstance(ctx, disnake.DMChannel):
					await ctx.send(self.bot.babel(ctx, 'confessions', 'missingchannelerr'))
				else:
					await ctx.response.send_message(self.bot.babel(ctx, 'confessions', 'missingchannelerr'), ephemeral=True)
		except Exception as e:
			if isinstance(ctx, disnake.DMChannel):
				await ctx.send(self.bot.babel(ctx, 'confessions', 'missingchannelerr'))
			else:
				await ctx.response.send_message(self.bot.babel(ctx, 'confessions', 'missingchannelerr'), ephemeral=True)
			raise e

	def findvettingchannel(self, guild):
		for channel in guild.channels:
			if self.bot.config.getint('confessions', str(guild.id)+'_'+str(channel.id), fallback=CHANNEL_TYPE.none) == CHANNEL_TYPE.vetting:
				return channel
		return None
	
	"""
		Checks
	"""
	def check_channel(self, guild_id:int, channel_id:int):
		# check if the selected channel is currently set
		channeltype = self.bot.config.getint('confessions', f"{guild_id}_{channel_id}", fallback=CHANNEL_TYPE.none)
		if channeltype in [CHANNEL_TYPE.traceable, CHANNEL_TYPE.untraceable, CHANNEL_TYPE.feedback]:
			return True
		return False

	def check_banned(self, guild_id:int, anonid:str):
		# check if user was banned
		if [i for i in self.bot.config.get('confessions', str(guild_id)+'_banned', fallback='').split(',') if anonid == i]:
			return False
		return True
	
	def check_imagesupport(self, guild_id:int, image:disnake.Attachment):
		# check if the user wants to send an image and if it's allowed.
		#TODO: also find links to images in message content
		if image and image.content_type.startswith('image'):
			if self.bot.config.getboolean('confessions', str(guild_id)+'_imagesupport', fallback=True):
				return True
			return False
	
	def check_spam(self, content:str):
		# check if the message appears to be spam
		for spamflag in self.bot.config.get('confessions', 'spam_flags', fallback=None).splitlines():
			if re.match(spamflag, content):
				return False
		return True

	"""
		Views
	"""

	class ChannelView(disnake.ui.View):
		def __init__(self, origin:disnake.Message, confessions, matches):
			super().__init__(timeout=30)
			self.origin = origin
			self.confessions = confessions
			for channel,channeltype in matches:
				self.channel_selector.append_option(disnake.SelectOption(label=f'{channel.name} (from {channel.guild.name})',
																						value=str(channel.id),
																						emoji=Confessions.channel_icons[channeltype]))
			self.channel_selector.placeholder = self.confessions.bot.babel(origin, 'confessions', 'channelprompt_placeholder')
			self.send_button.label = self.confessions.bot.babel(origin, 'confessions', 'channelprompt_button_send')
		
		@disnake.ui.select(min_values=1, max_values=1)
		async def channel_selector(self, select:disnake.ui.Select, inter:disnake.Interaction):
			self.send_button.disabled = False
			await inter.response.edit_message(content=self.confessions.bot.babel(inter, 'confessions', 'channelprompted', channel=f"<#{select._selected_values[0]}>"), view=self)

		@disnake.ui.button(disabled=True, style=disnake.ButtonStyle.primary)
		async def send_button(self, button:disnake.ui.Button, inter:disnake.Interaction):
			channel = await self.confessions.bot.fetch_channel(int(self.channel_selector.values[0]))

			anonid = self.confessions.get_anonid(channel.guild.id, inter.author.id)
			lead = ""
			if self.confessions.bot.config.getint('confessions', f"{channel.guild.id}_{channel.id}") != CHANNEL_TYPE.untraceable:
				lead = f"**[Anon-*{anonid}*]**"

			if not self.confessions.check_banned(channel.guild.id, anonid):
				await inter.response.send_message(self.bot.babel(inter, 'confessions', 'nosendbanned'))
				return
			
			content = self.origin.content
			image = self.origin.attachments[0] if self.origin.attachments else None
			if image:
				if not self.confessions.check_imagesupport(channel.guild.id, image):
					await inter.response.send_message(self.bot.babel(inter, 'confessions', 'nosendimages'))
					return

			if self.confessions.findvettingchannel(channel.guild):
				await inter.response.send_message("This guild has vetting enabled. Vetting isn't currently supported on ConfessionBeta.")
				return

			embed = self.confessions.generate_confession(anonid, lead, content, image.url if image else None)

			self.channel_selector.disabled = True
			self.send_button.disabled = True
			self.send_button.label = self.confessions.bot.babel(inter, 'confessions', 'channelprompt_button_sent')
			await self.confessions.send_confession(inter, channel, embed)
			await inter.response.edit_message(content=self.confessions.bot.babel(inter, 'confessions', 'confession_sent_channel', channel=channel.mention), view=self)

	class PendingConfessionButtons(disnake.ui.View):
		def __init__(self, confessor: disnake.Member):
			super().__init__(timeout=None)
			self.add_item(
					disnake.ui.Button(
							label="✅",
							style=disnake.ButtonStyle.green,
							custom_id=f"pending_confession_approve_{confessor.id}"
					)
			)
			self.add_item(
					disnake.ui.Button(
							label="❎",
							style=disnake.ButtonStyle.red,
							custom_id=f"pending_confession_deny_{confessor.id}"
					)
			)
			
	class ConfessionModal(disnake.ui.Modal):
		def __init__(self, confessions, target:disnake.User, image:disnake.Attachment):
			super().__init__(
				title = confessions.bot.babel(target, 'confessions', 'editor_title'),
				custom_id = "confession_modal",
				components = [
					disnake.ui.TextInput(
						label = confessions.bot.babel(target, 'confessions', 'editor_message_label'),
						placeholder = confessions.bot.babel(target, 'confessions', 'editor_message_placeholder'),
						custom_id = "content",
						style = disnake.enums.TextInputStyle.paragraph,
						min_length = 1
					)
				]
			)

			self.confessions = confessions
			self.image = image

		async def callback(self, inter:disnake.Interaction):
			if not self.confessions.check_spam(inter.text_values['content']):
				await inter.response.send_message(self.confessions.bot.babel(inter, 'confessions', 'nospam'), ephemeral=True)
				return
			
			anonid = self.confessions.get_anonid(inter.guild.id, inter.author.id)
			lead = ""
			if self.confessions.bot.config.getint('confessions', str(inter.guild.id)+'_'+str(inter.channel_id)) != CHANNEL_TYPE.untraceable:
				lead = f"**[Anon-*{anonid}*]**"

			embed = self.confessions.generate_confession(anonid, lead, inter.text_values['content'], self.image.url if self.image else None)

			await inter.response.send_message("Done, your message is below.", ephemeral=True)
			await self.confessions.send_confession(inter, inter.channel, embed)

	"""
		Events
	"""

	@commands.Cog.listener('on_raw_reaction_add')
	async def vetting_reaction(self, data:disnake.RawReactionActionEvent):
		if data.event_type == 'REACTION_ADD' and data.member and data.member != self.bot.user and\
				'pending_vetting_'+str(data.message_id) in self.bot.config['confessions']:
			if (data.member.guild_permissions.ban_members or \
					str(data.member.id) in self.bot.config.get('confessions', str(data.member.guild.id)+'_promoted', fallback='').split(',')) and \
					str(data.emoji) in ['✅','❎']:
				pendingconfession = PendingConfession(self.bot.config['confessions']['pending_vetting_'+str(data.message_id)])
				try:
					#TODO: this could be optimized with some refactoring
					await pendingconfession.fetch(self.bot)
					vetchannel = await self.bot.fetch_channel(data.channel_id)
					vetmessage = await vetchannel.fetch_message(data.message_id)
				except Exception as e:
					print("Failed to fetch required channels or guilds related to a confession (event);\n"+str(e))
					pendingconfession.failures += 1
					channel = await self.bot.fetch_channel(data.channel_id)
					if pendingconfession.failures > 5:
						self.bot.config.remove_option('confessions', 'pending_vetting_'+str(data.message_id))
						msg = await channel.fetch_message(data.message_id)
						await msg.delete()
					else:
						self.bot.config['confessions']['pending_vetting_'+str(data.message_id)] = str(pendingconfession)
						await channel.send(self.bot.babel(data.member, 'confessions', 'vettingrequiredmissing'))
				else:
					await self.on_confession_vetted(vetmessage, pendingconfession, data.emoji, data.member)
			else:
				channel = await self.bot.fetch_channel(data.channel_id)
				message = await channel.fetch_message(data.message_id)
				await message.remove_reaction(data.emoji, data.member)
        
	async def on_confession_vetted(self, vetmessage:disnake.Message, pendingconfession:PendingConfession, emoji:disnake.Emoji, voter:disnake.Member):
		anonid = self.get_anonid(pendingconfession.targetchannel.guild.id, pendingconfession.choicemsg.channel.recipient.id)
		lead = ""
		if self.bot.config.getint('confessions', str(pendingconfession.targetchannel.guild.id)+'_'+str(pendingconfession.targetchannel_id)) != CHANNEL_TYPE.untraceable:
			lead = f"**[Anon-*{anonid}*]**"

		embed = self.generate_confession(anonid, lead, pendingconfession.content, pendingconfession.image)
		accepted = True if str(emoji) == '✅' else False
		
		self.bot.config.remove_option('confessions', 'pending_vetting_'+str(vetmessage.id))
		self.bot.config.save()
		
		await vetmessage.edit(content=self.bot.babel(voter, 'confessions', 'vetaccepted' if accepted else 'vetdenied', channel=pendingconfession.targetchannel.mention), embed=embed)
		await pendingconfession.choicemsg.remove_reaction('💭', self.bot.user)
		await pendingconfession.choicemsg.add_reaction('✅' if accepted else '❎')
		if accepted:
			await self.send_confession(pendingconfession.choicechannel, pendingconfession.targetchannel, embed)

	@commands.Cog.listener('on_ready')
	async def reaction_catchup(self):
		changed = 0
		for option in (o for o in self.bot.config['confessions'] if o.startswith('pending_vetting_')):
			pendingconfession = PendingConfession(self.bot.config['confessions'][option])
			try:
				targetchannel = await self.bot.fetch_channel(pendingconfession.targetchannel_id)
				vetchannel = self.findvettingchannel(targetchannel.guild)
				vetmessage = await vetchannel.fetch_message(int(option[16:]))
			except Exception as e:
				print("Failed to fetch required channels or guilds related to a confession (catchup);\n"+str(e))
				pendingconfession.failures += 1
				if pendingconfession.failures > 5:
					self.bot.config.remove_option('confessions', option)
					changed += 1
				else:
					self.bot.config['confessions'][option] = str(pendingconfession)
					changed += 1
				
				# optimization to reduce the number of writes
				if changed > 100:
					self.bot.config.save()
					changed = 0
			if changed:
				self.bot.config.save()
				changed = 0
			else:
				for reaction in vetmessage.reactions:
					async for voter in reaction.users():
						if voter != self.bot.user:
							if isinstance(voter, disnake.Member):
								data = disnake.RawReactionActionEvent({'message_id':vetmessage.id, 'channel_id':vetchannel.id, 'user_id':voter.id}, reaction.emoji, 'REACTION_ADD')
								await self.vetting_reaction(data)

	@commands.Cog.listener('on_message')
	async def confession_request(self, msg:disnake.Message):
		ctx = await self.bot.get_context(msg)
		if ctx.prefix is not None:
			return
		if isinstance(msg.channel, disnake.DMChannel) and\
				msg.author != self.bot.user:
			if msg.channel in self.ignore:
				self.ignore.remove(msg.channel)
				return
			if msg.author in self.confession_cooldown and self.confession_cooldown[msg.author] > time.time():
				return
			else:
				self.confession_cooldown[msg.author] = time.time() + self.bot.config.getint('confessions', 'confession_cooldown', fallback=1)
			
			if 'Log' in self.bot.cogs:
				await self.bot.cogs['Log'].log_misc(msg)

			matches,vetting = self.listavailablechannels(msg.author)

			if not self.bot.is_ready():
				await msg.channel.send(self.bot.babel(msg, 'confessions', 'cachebuilding', s=self.bot.config['confessions']['starttime']))

			if not matches:
				await msg.channel.send(self.bot.babel(msg, 'confessions', 'inaccessible'))
				return
			
			if not self.check_spam(msg.content):
				await msg.reply(self.bot.babel(msg, 'confessions', 'nospam'))
				return
			
			await msg.reply(self.bot.babel(msg, 'confessions', 'channelprompt'), view=self.ChannelView(msg, self, matches))

			"""
			embed = self.generate_confession(anonid, lead, msg.content, image)

			vettingchannel = self.findvettingchannel(targetchannel.guild)
			status = '💭' if vettingchannel else '✅'
			
			await choicemsg.add_reaction(status)

			if vettingchannel:
				vetembed = self.generate_confession(anonid, lead if lead else f"**[Anon-*{anonid}*]**", msg.content, image)

				vetmessage = await vettingchannel.send(self.bot.babel(targetchannel.guild,'confessions','vetmessagecta',channel=targetchannel.mention),embed=vetembed)
				await vetmessage.add_reaction('✅')
				await vetmessage.add_reaction('❎')

				# Store pending message details for handling after vetting
				pendingconfession = PendingConfession(vetmessage=vetmessage,
				choicemsg=choicemsg,
				targetchannel=targetchannel,
				content=msg.content,
				image=image)
				
				self.bot.config['confessions']['pending_vetting_'+str(vetmessage.id)] = str(pendingconfession)
				
				# Store offline in case of restarts or power failures
				self.bot.config.save()
				
				return

			await self.send_confession(msg.channel, targetchannel, embed)"""


	"""
		Slash commands
	"""

	@commands.cooldown(1, 1)
	@commands.slash_command(description="Send an anonymous message")
	async def confess(self, inter: disnake.GuildCommandInteraction,
		content:Optional[str]=commands.Param(None, description="The text of your anonymous message, leave blank for a paragraph editor"),
		image:Optional[disnake.Attachment]=commands.Param(None, description="Include an image, appears below the content")
	):
		if not self.check_channel(inter.guild_id, inter.channel_id):
			await inter.response.send_message(self.bot.babel(inter, 'confessions', 'nosendchannel'), ephemeral=True)
			return
		
		anonid = self.get_anonid(inter.guild_id, inter.author.id)
		lead = ""
		if self.bot.config.getint('confessions', f"{inter.guild.id}_{inter.channel_id}") != CHANNEL_TYPE.untraceable:
			lead = f"**[Anon-*{anonid}*]**"

		if not self.check_banned(inter.guild_id, anonid):
			await inter.response.send_message(self.bot.babel(inter, 'confessions', 'nosendbanned'), ephemeral=True)
			return
		
		if image:
			if not self.check_imagesupport(inter.channel_id, image):
				await inter.response.send_message(self.bot.babel(inter, 'confessions', 'nosendimages'), ephemeral=True)
				return

		if self.findvettingchannel(inter.guild):
			await inter.response.send_message("This guild has vetting enabled. Vetting isn't currently supported on ConfessionBeta.", ephemeral=True)
			return

		if content or image:
			if content is None: content=''

			if not self.check_spam(content):
				await inter.response.send_message(self.bot.babel(inter, 'confessions', 'nospam'), ephemeral=True)
				return
			
			embed = self.generate_confession(anonid, lead, content, image.url if image else None)
			await inter.response.send_message(self.bot.babel(inter, 'confessions', 'confession_sent_below'), ephemeral=True)
			await self.send_confession(inter, inter.channel, embed)

		else:
			await inter.response.send_modal(modal=self.ConfessionModal(confessions=self, target=inter.author, image=image))

	"""
		Commands
	"""

	@commands.guild_only()
	@commands.command()
	async def set(self, ctx:commands.Context, mode:str):
		self.bot.cogs['Auth'].admins(ctx)
		type = CHANNEL_TYPE.__dict__.get(mode, CHANNEL_TYPE.invalid)
		if type==CHANNEL_TYPE.invalid:
			raise commands.BadArgument()
		elif type==CHANNEL_TYPE.none:
			wastype = self.bot.config.getint('confessions', str(ctx.guild.id)+'_'+str(ctx.channel.id), fallback=CHANNEL_TYPE.none)
			if wastype == CHANNEL_TYPE.none:
				await ctx.reply(self.bot.babel(ctx, 'confessions', 'unsetfailure'))
				return
			self.bot.config.remove_option('confessions', str(ctx.guild.id)+'_'+str(ctx.channel.id))
		else:
			self.bot.config['confessions'][str(ctx.guild.id)+'_'+str(ctx.channel.id)] = str(type)
		self.bot.config.save()

		await ctx.reply(self.bot.babel(ctx, 'confessions', ('setsuccess'+str(type) if type>=0 else 'unsetsuccess')) + \
										self.bot.babel(ctx, 'confessions', 'setundo' if type>=0 else 'unsetundo'))

	@commands.command()
	async def list(self, ctx):
		matches,vetting = self.listavailablechannels(ctx.author)
		# warn users when the channel list isn't complete
		if not self.bot.is_ready():
			#TODO: average ready time and report the real figure
			await ctx.reply(self.bot.babel(ctx,'confessions','cachebuilding', s=self.bot.config['confessions']['starttime']))
		elif len(matches) == 0:
			await ctx.reply(self.bot.babel(ctx,'confessions','inaccessiblelocal' if isinstance(ctx.author, disnake.Member) else 'inaccessible'))
		else:
			await ctx.reply((self.bot.babel(ctx,'confessions','listtitlelocal') if isinstance(ctx.author, disnake.Member) else self.bot.babel(ctx,'confessions','listtitle')) + \
											 '\n'+self.generate_list(ctx.author, matches, vetting))

	@commands.guild_only()
	@commands.command()
	async def shuffle(self, ctx, one:str = None):
		if str(ctx.author.id) not in self.bot.config.get('confessions', str(ctx.guild.id)+'_promoted', fallback='').split(','):
			self.bot.cogs['Auth'].mods(ctx)
		if one:
			await ctx.reply(self.bot.babel(ctx, 'confessions', 'shuffleobsoleteone'))
		else:
			if str(ctx.guild.id)+'_banned' in self.bot.config['confessions']:
				msg = await ctx.reply(self.bot.babel(ctx, 'confessions', 'shufflebanresetwarning'))
				def check(m):
					return m.channel == ctx.channel and m.author == ctx.author and m.content.lower() == 'yes'
				try:
					await self.bot.wait_for('message', check=check, timeout=30)
				except asyncio.TimeoutError:
					await ctx.send(self.bot.babel(ctx, 'confessions', 'timeouterror'))
				else:
					self.bot.config.remove_option('confessions', str(ctx.guild.id)+'_banned')
			
			shuffle = self.bot.config.getint('confessions', str(ctx.guild.id)+'_shuffle', fallback=0)
			self.bot.config.set('confessions', str(ctx.guild.id)+'_shuffle', str(shuffle + 1))
			self.bot.config.save()

			await ctx.reply(self.bot.babel(ctx, 'confessions', 'shufflesuccess'))

	@commands.guild_only()
	@commands.command()
	async def imagesupport(self, ctx:commands.Context, cmd:str):
		self.bot.cogs['Auth'].admins(ctx)
		if cmd=='enable':
			if self.bot.config.getboolean('confessions', str(ctx.guild.id)+'_imagesupport', fallback=True):
				await ctx.reply(self.bot.babel(ctx, 'confessions', 'imagesupportalreadyenabled'))
			else:
				self.bot.config['confessions'][str(ctx.guild.id)+'_imagesupport'] = 'True'
				self.bot.config.save()
				await ctx.reply(self.bot.babel(ctx, 'confessions', 'imagesupportenabled'))
		elif cmd=='disable':
			if not self.bot.config.getboolean('confessions', str(ctx.guild.id)+'_imagesupport', fallback=True):
				await ctx.reply(self.bot.babel(ctx, 'confessions', 'imagesupportalreadydisabled'))
			else:
				self.bot.config['confessions'][str(ctx.guild.id)+'_imagesupport'] = 'False'
				self.bot.config.save()
				await ctx.reply(self.bot.babel(ctx, 'confessions', 'imagesupportdisabled'))

	@commands.guild_only()
	@commands.command()
	async def ban(self, ctx:commands.Context, anonid:str=None):
		if str(ctx.author.id) not in self.bot.config.get('confessions', str(ctx.guild.id)+'_promoted', fallback='').split(','):
			self.bot.cogs['Auth'].mods(ctx)

		banlist = self.bot.config.get('confessions', str(ctx.guild.id)+'_banned', fallback='')
		if anonid is None:
			if banlist:
				await ctx.reply(self.bot.babel(ctx, 'confessions', 'banlist') + '\n```' + '\n'.join(banlist.split(',')) + '```')
			else:
				await ctx.reply(self.bot.babel(ctx, 'confessions', 'emptybanlist'))
			return
		
		unban = anonid.startswith('-')
		if unban: anonid = anonid[1:]
		
		anonid = anonid.lower()
		if len(anonid)>6:
			anonid = anonid[-6:]
		try: int(anonid, 16)
		except ValueError:
			await ctx.reply(self.bot.babel(ctx, 'confessions', 'invalidanonid'))
			return
		if anonid in banlist and not unban:
			await ctx.reply(self.bot.babel(ctx, 'confessions', 'doublebananonid'))
			return
		
		if unban:
			fullid = [i for i in banlist.split(',') if anonid in i][0]
			self.bot.config['confessions'][str(ctx.guild.id)+'_banned'] = banlist.replace(fullid+',','')
		else:
			self.bot.config['confessions'][str(ctx.guild.id)+'_banned'] = banlist + anonid + ','
		self.bot.config.save()

		await ctx.reply(self.bot.babel(ctx, 'confessions', ('un' if unban else '')+'bansuccess', user=anonid))

	@commands.guild_only()
	@commands.command()
	async def botmod(self, ctx:commands.Context, target:str=None):
		self.bot.cogs['Auth'].admins(ctx)

		modlist = self.bot.config.get('confessions', str(ctx.guild.id)+'_promoted', fallback='')
		if target is None:
			if modlist:
				await ctx.reply(self.bot.babel(ctx, 'confessions', 'botmodlist') + '\n```\n' + '\n'.join(modlist.split(',')) + '```')
			else:
				await ctx.reply(self.bot.babel(ctx, 'confessions', 'botmodemptylist'))
		elif ctx.message.mentions:
			revoke = target.startswith('-')
			botmodee = [m for m in ctx.message.mentions if m != self.bot.user][0]
			if botmodee.bot:
				await ctx.reply(self.bot.babel(ctx, 'confessions', 'botmodboterr'))
				return
			if botmodee.guild_permissions.ban_members:
				await ctx.reply(self.bot.babel(ctx, 'confessions', 'botmodmoderr'))
				return

			if revoke:
				if str(botmodee.id) in modlist.split(','):
					self.bot.config['confessions'][str(ctx.guild.id)+'_promoted'] = modlist.replace(str(botmodee.id)+',','')
					self.bot.config.save()
					await ctx.reply(self.bot.babel(ctx, 'confessions', 'botmoddemotesuccess', user=botmodee.name))
				else:
					await ctx.reply(self.bot.babel(ctx, 'confessions', 'botmoddemoteerr'))
			else:
				if str(botmodee.id) not in modlist.split(','):
					self.bot.config['confessions'][str(ctx.guild.id)+'_promoted'] = modlist + str(botmodee.id)+','
					self.bot.config.save()
					await ctx.reply(self.bot.babel(ctx, 'confessions', 'botmodsuccess', user=botmodee.name))
				else:
					await ctx.reply(self.bot.babel(ctx, 'confessions', 'rebotmoderr'))
		else:
			raise commands.BadArgument()

	"""
		Cleanup
	"""

	@commands.Cog.listener('on_guild_leave')
	async def guild_cleanup(self, guild:disnake.Guild):
		for option in self.bot.config['confessions']:
			if option.startswith(str(guild.id)+'_'):
				self.bot.config.remove_option('confessions', option)
		self.bot.config.save()

	@commands.Cog.listener('on_guild_channel_delete')
	async def channel_cleanup(self, channel:disnake.TextChannel):
		for option in self.bot.config['confessions']:
			if option == str(channel.guild.id)+'_'+str(channel.id):
				self.bot.config.remove_option('confessions', option)
				break
		self.bot.config.save()

def setup(bot):
	bot.add_cog(Confessions(bot))
