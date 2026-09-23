"""Tests for msg_parse.py (synthetic XML samples only).

Run: ./venv/bin/python -m unittest discover -s tests -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import msg_parse as P  # noqa: E402
import chats  # noqa: E402


def app(sub):
    return 49 | (sub << 32)


def appmsg(inner, app_type, tail=""):
    return (f'<?xml version="1.0"?>\n<msg>\n\t<appmsg appid="" sdkver="0">\n{inner}\n'
            f'\t\t<type>{app_type}</type>\n\t</appmsg>\n{tail}</msg>\n')


NAMES = {"wxid_alice": "Alice", "wxid_bob": "Bob", "wxid_me": "我"}


def resolve(u):
    return NAMES.get(u)


QUOTE_TEXT = appmsg("""
		<title>我也觉得</title>
		<refermsg>
			<type>1</type>
			<svrid>1234567890123</svrid>
			<fromusr>demo@chatroom</fromusr>
			<chatusr>wxid_alice</chatusr>
			<displayname>Alice in group</displayname>
			<content>今天天气
真好</content>
			<createtime>1700000000</createtime>
		</refermsg>""", 57)

QUOTE_IMAGE = appmsg("""
		<title>这张好看</title>
		<refermsg>
			<type>3</type>
			<svrid>42</svrid>
			<fromusr>wxid_stranger</fromusr>
			<displayname>Stranger</displayname>
			<content>wxid_stranger:
&lt;?xml version="1.0"?&gt;
&lt;msg&gt;&lt;img aeskey="00" length="10" /&gt;&lt;/msg&gt;
</content>
		</refermsg>""", 57)

QUOTE_LINK = appmsg("""
		<title>看看这个</title>
		<refermsg>
			<type>49</type>
			<fromusr>wxid_bob</fromusr>
			<displayname>Bobby</displayname>
			<content>&lt;msg&gt;&lt;appmsg&gt;&lt;title&gt;有趣的文章&lt;/title&gt;&lt;type&gt;5&lt;/type&gt;&lt;url&gt;https://example.com/a&lt;/url&gt;&lt;/appmsg&gt;&lt;/msg&gt;</content>
		</refermsg>""", 57)

QUOTE_OF_QUOTE = appmsg("""
		<title>第三层</title>
		<refermsg>
			<type>49</type>
			<fromusr>wxid_me</fromusr>
			<content>&lt;msg&gt;&lt;appmsg&gt;&lt;title&gt;第二层回复&lt;/title&gt;&lt;type&gt;57&lt;/type&gt;&lt;refermsg&gt;&lt;type&gt;1&lt;/type&gt;&lt;content&gt;第一层&lt;/content&gt;&lt;/refermsg&gt;&lt;/appmsg&gt;&lt;/msg&gt;</content>
		</refermsg>""", 57)

LINK = appmsg("""
		<title>活动通知：示例讲座（01月29日）</title>
		<des>嘉宾：某某
时间：晚上八点</des>
		<action>view</action>
		<url>https://mp.weixin.qq.com/s?__biz=AAA&amp;mid=1&amp;idx=1#rd</url>
		<sourceusername>gh_demo</sourceusername>
		<sourcedisplayname>示例公众号</sourcedisplayname>""", 5,
                "\t<fromusername>wxid_alice</fromusername>\n\t<appinfo><version>1</version><appname /></appinfo>\n")

VIDEO_LINK = ('<msg><appmsg appid="wx000"  sdkver="0"><title>Cute Cow</title>'
              '<des>@someone\'s note</des><type>4</type>'
              '<url>https://www.example.org/item/1?a=1&amp;b=2</url></appmsg>'
              '<appinfo><version>0</version><appname>示例App</appname></appinfo></msg>')

FILE = appmsg("""
		<title>需求说明.docx</title>
		<appattach>
			<totallen>20509</totallen>
			<fileext>docx</fileext>
		</appattach>""", 6)

MINI = appmsg("""
		<title>限时秒杀</title>
		<des>示例品牌</des>
		<sourceusername>gh_x@app</sourceusername>
		<sourcedisplayname>示例商城</sourcedisplayname>
		<weappinfo><username>gh_x@app</username><type>2</type></weappinfo>""", 33)

REDPACKET = appmsg("""
		<title>微信红包</title>
		<des>我给你发了一个红包，赶紧去拆!</des>
		<wcpayinfo>
			<paysubtype>0</paysubtype>
			<receivertitle><![CDATA[生日快乐，万事如意]]></receivertitle>
			<sendertitle><![CDATA[生日快乐，万事如意]]></sendertitle>
			<scenetext><![CDATA[微信红包]]></scenetext>
			<redenvelopetype>1</redenvelopetype>
		</wcpayinfo>""", 2001)

REDPACKET_OLD = ('<msg><appmsg appid="" sdkver=""><des><![CDATA[我给你发了一个红包，赶紧去拆! '
                 '祝：恭喜发财，大吉大利！]]></des><type>2001</type><title><![CDATA[微信红包]]></title>'
                 '<wcpayinfo><scenetext>微信红包</scenetext></wcpayinfo></appmsg></msg>')

GROUP_COLLECT = appmsg("""
		<title>聚餐AA</title>
		<wcpayinfo><paysubtype>1</paysubtype><scenetext>群收款</scenetext></wcpayinfo>""", 2001)


def transfer(pst, fee="￥350.00", memo=""):
    return appmsg(f"""
		<title><![CDATA[微信转账]]></title>
		<des><![CDATA[收到转账350.00元。如需收钱，请点此升级至最新版本]]></des>
		<wcpayinfo>
			<paysubtype>{pst}</paysubtype>
			<feedesc><![CDATA[{fee}]]></feedesc>
			<pay_memo><![CDATA[{memo}]]></pay_memo>
			<receiver_username><![CDATA[wxid_bob]]></receiver_username>
		</wcpayinfo>""", 2000)


CHANNELS = appmsg("""
		<title>当前微信版本不支持展示该内容，请升级至最新版本。</title>
		<finderFeed>
			<nickname>示例作者</nickname>
			<desc>毛衣这样挂
不变形 #生活小妙招</desc>
			<mediaCount>1</mediaCount>
		</finderFeed>""", 51)

LIVE = appmsg("""
		<title>当前版本不支持展示该内容，请升级至最新版本。</title>
		<finderLive><nickname>示例直播间</nickname><desc>直播标题</desc></finderLive>""", 63)


def voip(msg, room_type):
    return (f'<voipmsg type="VoIPBubbleMsg"><VoIPBubbleMsg><msg><![CDATA[{msg}]]></msg>\n'
            f'<room_type>{room_type}</room_type>\n<red_dot>false</red_dot>\n'
            f'<roomid>1</roomid>\n<duration>0</duration>\n</VoIPBubbleMsg></voipmsg>')


VOIP_INVITE = ('<voipinvitemsg><roomid>1</roomid><key>2</key><status>2</status>'
               '<invitetype>1</invitetype></voipinvitemsg><voipextinfo><recvtime>1'
               '</recvtime></voipextinfo>')

CARD = ('wxid_alice:\n<?xml version="1.0"?>\n<msg bigheadimgurl="http://example.com/0" '
        'username="v3_0000@stranger" nickname="示例好友" alias="" certflag="0" certinfo="" '
        'sex="0" />\n')
CARD_OFFICIAL = ('<?xml version="1.0"?>\n<msg username="gh_0000" nickname="示例医院" '
                 'certflag="24" certinfo="示例医院(附属)" brandFlags="0" />\n')

RECORD_INNER = ("<recordinfo><title>群聊的聊天记录</title><datalist count=\"2\">"
                "<dataitem datatype=\"1\"><sourcename>Alice</sourcename>"
                "<sourcetime>2023-6-20 19:45</sourcetime><datadesc>第一条</datadesc></dataitem>"
                "<dataitem datatype=\"2\"><sourcename>Bob</sourcename>"
                "<sourcetime>2023-6-20 19:46</sourcetime></dataitem>"
                "</datalist></recordinfo>")
CHAT_HISTORY = appmsg(f"""
		<title>Alice和Bob的聊天记录</title>
		<des>Alice: 你好
Bob: [图片]</des>
		<url>https://support.weixin.qq.com/cgi-bin/mmsupport-bin/readtemplate?t=page/favorite_record__w_unsupport</url>
		<recorditem><![CDATA[<recordinfo><info>Alice: 你好</info><datalist count="5">
<dataitem datatype="1" dataid="a"><sourcename>Alice</sourcename><sourcetime>2023-6-20 19:45</sourcetime><srcMsgCreateTime>1687261500</srcMsgCreateTime><datadesc>你好
第二行</datadesc></dataitem>
<dataitem datatype="2" dataid="b"><sourcename>Bob</sourcename><sourcetime>2023-6-20 19:46</sourcetime><cdnthumburl>305702</cdnthumburl></dataitem>
<dataitem datatype="5" dataid="c"><sourcename>Bob</sourcename><sourcetime>2023-6-20 19:47</sourcetime><datatitle>一篇文章</datatitle><link>https://example.com/x</link></dataitem>
<dataitem datatype="8" dataid="d"><sourcename>Alice</sourcename><sourcetime>2023-6-20 19:48</sourcetime><datatitle>a.java</datatitle><datasize>2048</datasize></dataitem>
<dataitem datatype="17" dataid="e"><sourcename>Alice</sourcename><sourcetime>2023-6-20 19:49</sourcetime><datatitle>群聊的聊天记录</datatitle><recordxml>{RECORD_INNER}</recordxml></dataitem>
</datalist></recordinfo>]]></recorditem>""", 19)

# Real-world quirk: an XML declaration after the root tag.
CHAT_HISTORY_DECL = ('wxid_bob:\n<msg><?xml version="1.0"?>\n<appmsg appid="" sdkver="0">'
                     '<title>聊天记录</title><type>19</type><recorditem><![CDATA[<recordinfo>'
                     '<datalist count="1"><dataitem datatype="4"><sourcename>Bob</sourcename>'
                     '<sourcetime>2024-1-1 10:00</sourcetime></dataitem></datalist></recordinfo>'
                     ']]></recorditem></appmsg></msg>')

PAT = appmsg("""
		<title>Alice tickled me</title>
		<patinfo>
			<fromusername>wxid_alice</fromusername>
			<chatusername>wxid_me</chatusername>
			<pattedusername>wxid_me</pattedusername>
			<template><![CDATA[${wxid_alice} tickled me]]></template>
		</patinfo>""", 62)

PAT_TEMPLATE_ONLY = appmsg("""
		<title></title>
		<patinfo>
			<fromusername>wxid_alice</fromusername>
			<pattedusername>wxid_bob</pattedusername>
			<template><![CDATA["${wxid_alice}" 拍了拍 "${wxid_bob}"]]></template>
		</patinfo>""", 62)

LOCATION = ('<?xml version="1.0"?>\n<msg>\n\t<location x="31.260204" y="120.747391" scale="15" '
            'label="示例市示例路1号" maptype="0" poiname="示例咖啡馆" fromusername="wxid_alice" />\n</msg>\n')

NOTICE = appmsg("""
		<announcement>&lt;group_notice_item /&gt;</announcement>
		<textannouncement>周六下午公园野餐
带垫子</textannouncement>""", 87)

MUSIC = appmsg("""
		<title>示例歌曲</title>
		<des>示例歌手</des>
		<url>https://music.example.com/song?id=1</url>
		<musicShareItem><mvSingerName>示例歌手</mvSingerName></musicShareItem>""", 76,
               "\t<appinfo><version>49</version><appname>示例音乐</appname></appinfo>\n")

SYS_TEMPLATE = """<sysmsg type="sysmsgtemplate">
	<sysmsgtemplate>
		<content_template type="tmpl_type_profile">
			<plain><![CDATA[]]></plain>
			<template><![CDATA[$username$ invited you and $names$ to the group chat]]></template>
			<link_list>
				<link name="username" type="link_profile">
					<memberlist><member><username><![CDATA[wxid_alice]]></username>
					<nickname><![CDATA[Alice Nick]]></nickname></member></memberlist>
				</link>
				<link name="names" type="link_profile">
					<memberlist>
						<member><username><![CDATA[wxid_x]]></username><nickname><![CDATA[Xavier]]></nickname></member>
						<member><username><![CDATA[wxid_bob]]></username><nickname><![CDATA[Bob Nick]]></nickname></member>
					</memberlist>
					<separator><![CDATA[, ]]></separator>
				</link>
			</link_list>
		</content_template>
	</sysmsgtemplate>
</sysmsg>"""

SYS_REVOKE = ('<?xml version="1.0"?><sysmsg type="revokemsg"><revokemsg><content>"Alice" '
              'recalled a message</content><revoketime>0</revoketime></revokemsg></sysmsg>')
SYS_PAYMSG = ('<?xml version="1.0"?>\n<sysmsg type="paymsg"><content><![CDATA[你有一笔待接收的'
              '<_wc_custom_link_ href="weixin://wxpay/transfer">转账</_wc_custom_link_>]]>'
              '</content></sysmsg>')
SYS_PAT = ('<sysmsg type="pat"><pat><fromusername>wxid_alice</fromusername>'
           '<pattedusername>wxid_bob</pattedusername><template><![CDATA["${wxid_alice}" '
           '拍了拍 "${wxid_bob}"]]></template></pat></sysmsg>')


class ParseTest(unittest.TestCase):
    def p(self, text, lt, res=resolve):
        return P.parse(text, lt, res)

    # ---------------------------------------------------------- quotes
    def test_quote_text(self):
        text, extra, kind = self.p(QUOTE_TEXT, app(57))
        self.assertEqual(kind, "quote")
        self.assertEqual(text, "我也觉得 [引用 Alice: 今天天气 真好]")
        self.assertEqual(extra["reply"], "我也觉得")
        self.assertEqual(extra["quote"], {"sender": "Alice", "text": "今天天气 真好",
                                          "kind": "text", "local_type": 1,
                                          "server_id": 1234567890123, "ts": 1700000000})

    def test_quote_sender_falls_back_to_displayname(self):
        text, extra, _ = self.p(QUOTE_TEXT, app(57), res=None)
        self.assertEqual(extra["quote"]["sender"], "Alice in group")
        text, extra, _ = self.p(QUOTE_IMAGE, app(57))
        self.assertEqual(text, "这张好看 [引用 Stranger: [图片]]")
        self.assertEqual(extra["quote"]["kind"], "image")

    def test_quote_nested_app(self):
        text, extra, _ = self.p(QUOTE_LINK, app(57))
        self.assertEqual(text, "看看这个 [引用 Bob: [链接] 有趣的文章 https://example.com/a]")
        self.assertEqual(extra["quote"]["kind"], "link")
        text, extra, _ = self.p(QUOTE_OF_QUOTE, app(57))
        self.assertEqual(text, "第三层 [引用 我: 第二层回复]")
        self.assertEqual(extra["quote"]["kind"], "quote")

    def test_quote_snippet_truncated(self):
        long = QUOTE_TEXT.replace("今天天气\n真好", "字" * 200)
        text, extra, _ = self.p(long, app(57))
        self.assertTrue(text.endswith("字…]"))
        self.assertEqual(len(extra["quote"]["text"]), 200)

    def test_quote_without_refermsg_and_malformed(self):
        self.assertEqual(self.p(appmsg("<title>只有回复</title>", 57), app(57))[0], "只有回复")
        # truncated XML falls back to the title regex
        self.assertEqual(self.p(QUOTE_TEXT[:QUOTE_TEXT.index("<refermsg>") + 40], app(57))[:2],
                         ("我也觉得", None))
        self.assertEqual(self.p(None, app(57))[0], "[引用消息]")

    # ---------------------------------------------------------- links etc.
    def test_link(self):
        text, extra, kind = self.p(LINK, app(5))
        url = "https://mp.weixin.qq.com/s?__biz=AAA&mid=1&idx=1#rd"
        self.assertEqual(kind, "link")
        self.assertEqual(text, f"[链接] 活动通知：示例讲座（01月29日） (示例公众号) {url}")
        self.assertEqual(extra, {"title": "活动通知：示例讲座（01月29日）", "url": url,
                                 "desc": "嘉宾：某某\n时间：晚上八点", "source": "示例公众号"})

    def test_video_link_sub4(self):
        text, extra, kind = self.p(VIDEO_LINK, app(4))
        self.assertEqual(kind, "link")
        self.assertEqual(text, "[链接] Cute Cow (示例App) https://www.example.org/item/1?a=1&b=2")
        self.assertEqual(extra["source"], "示例App")

    def test_file(self):
        text, extra, kind = self.p(FILE, app(6))
        self.assertEqual((text, kind), ("[文件] 需求说明.docx (20.0 KB)", "file"))
        self.assertEqual(extra, {"title": "需求说明.docx", "size": 20509, "ext": "docx"})
        self.assertEqual(P.human_size(512), "512 B")
        self.assertEqual(P.human_size(3 * 1024 ** 3), "3.0 GB")

    def test_miniprogram(self):
        text, extra, kind = self.p(MINI, app(33))
        self.assertEqual((text, kind), ("[小程序] 限时秒杀 (示例商城)", "miniprogram"))
        self.assertEqual(extra["source"], "示例商城")

    def test_music(self):
        text, extra, kind = self.p(MUSIC, app(76))
        self.assertEqual((text, kind), ("[音乐] 示例歌曲 - 示例歌手 (示例音乐)", "music"))
        self.assertEqual(extra["url"], "https://music.example.com/song?id=1")

    def test_notice(self):
        text, extra, kind = self.p(NOTICE, app(87))
        self.assertEqual((text, kind), ("[群公告] 周六下午公园野餐 带垫子", "notice"))
        self.assertEqual(extra["text"], "周六下午公园野餐\n带垫子")

    # ---------------------------------------------------------- money
    def test_redpacket(self):
        text, extra, kind = self.p(REDPACKET, app(2001))
        self.assertEqual((text, kind), ("[红包] 生日快乐，万事如意", "redpacket"))
        self.assertEqual(extra, {"greeting": "生日快乐，万事如意", "scene": "微信红包"})
        self.assertEqual(self.p(REDPACKET_OLD, app(2001))[0], "[红包] 恭喜发财，大吉大利！")
        self.assertEqual(self.p(GROUP_COLLECT, app(2001))[0], "[群收款] 聚餐AA")

    def test_transfer(self):
        cases = [(1, "[转账] ¥350.00", "sent"), (8, "[转账] ¥350.00", "sent"),
                 (3, "[转账] ¥350.00 已收款", "received"), (4, "[转账] ¥350.00 已退还", "refunded"),
                 (10, "[转账] ¥350.00 已过期", "expired"), (77, "[转账] ¥350.00", "unknown")]
        for pst, want, status in cases:
            text, extra, kind = self.p(transfer(pst), app(2000))
            self.assertEqual((text, kind, extra["status"], extra["paysubtype"]),
                             (want, "transfer", status, pst))
        text, extra, _ = self.p(transfer(3, memo="生活费"), app(2000))
        self.assertEqual(text, "[转账] ¥350.00 已收款 · 生活费")
        self.assertEqual(extra["memo"], "生活费")
        # amount from <des> when feedesc is missing
        self.assertEqual(self.p(transfer(3, fee=""), app(2000))[0], "[转账] ¥350.00 已收款")

    # ---------------------------------------------------------- channels
    def test_channels(self):
        text, extra, kind = self.p(CHANNELS, app(51))
        self.assertEqual(kind, "channels")
        self.assertEqual(text, "[视频号] 示例作者: 毛衣这样挂 不变形 #生活小妙招")
        self.assertEqual(extra["author"], "示例作者")
        self.assertFalse(extra["live"])
        text, extra, _ = self.p(LIVE, app(63))
        self.assertEqual(text, "[视频号直播] 示例直播间: 直播标题")
        self.assertTrue(extra["live"])

    # ---------------------------------------------------------- calls
    def test_calls(self):
        cases = [(voip("Duration: 48:31", 1), "[语音通话] 通话时长 48:31", "completed", 2911),
                 (voip("通话时长 01:00:05", 0), "[视频通话] 通话时长 01:00:05", "completed", 3605),
                 (voip("Call canceled by caller", 0), "[视频通话] 已取消", "cancelled", None),
                 (voip("Call not answered", 1), "[语音通话] 未接听", "missed", None),
                 (voip("Line busy. Call not received.", 1), "[语音通话] 忙线未接听", "busy", None),
                 (voip("对方已拒绝", 1), "[语音通话] 已拒绝", "declined", None),
                 (voip("Already answered elsewhere", 0), "[视频通话] 已在其他设备接听",
                  "answered_elsewhere", None),
                 (voip("Something new", 1), "[语音通话] Something new", "unknown", None),
                 (VOIP_INVITE, "[语音通话] 通话邀请", "invite", None)]
        for content, want, status, dur in cases:
            text, extra, kind = self.p(content, 50)
            self.assertEqual((text, kind, extra["status"], extra.get("duration")),
                             (want, "call", status, dur), content)
        self.assertEqual(self.p(None, 50)[0], "[通话]")

    # ---------------------------------------------------------- cards
    def test_cards(self):
        text, extra, kind = self.p(CARD, 42)
        self.assertEqual((text, kind), ("[名片] 示例好友", "card"))
        self.assertEqual(extra, {"nickname": "示例好友", "card_type": "person"})
        text, extra, _ = self.p(CARD_OFFICIAL, 42)
        self.assertEqual(text, "[公众号名片] 示例医院")
        self.assertEqual(extra["certinfo"], "示例医院(附属)")
        self.assertEqual(self.p(CARD.replace("v3_0000@stranger", "1@openim"), 42)[0],
                         "[企业微信名片] 示例好友")
        self.assertEqual(self.p("garbage", 42)[0], "[名片]")

    # ---------------------------------------------------------- chat history
    def test_chat_history(self):
        text, extra, kind = self.p(CHAT_HISTORY, app(19))
        self.assertEqual((text, kind), ("[聊天记录] Alice和Bob的聊天记录 (5条)", "chat_history"))
        items = extra["items"]
        self.assertEqual([(i["sender"], i["kind"], i["text"]) for i in items], [
            ("Alice", "text", "你好\n第二行"),
            ("Bob", "image", "[图片]"),
            ("Bob", "link", "[链接] 一篇文章"),
            ("Alice", "file", "[文件] a.java (2.0 KB)"),
            ("Alice", "chat_history", "[聊天记录] 群聊的聊天记录"),
        ])
        self.assertEqual(items[0]["ts"], 1687261500)
        self.assertEqual(items[1]["time"], "2023-6-20 19:46")
        self.assertEqual(items[2]["url"], "https://example.com/x")
        self.assertEqual([(i["sender"], i["text"]) for i in items[4]["items"]],
                         [("Alice", "第一条"), ("Bob", "[图片]")])

    def test_chat_history_decl_after_root(self):
        text, extra, _ = self.p(CHAT_HISTORY_DECL, app(19))
        self.assertEqual(text, "[聊天记录] 聊天记录 (1条)")
        self.assertEqual(extra["items"][0]["text"], "[视频]")

    def test_chat_history_bad_recorditem(self):
        bad = appmsg("<title>记录</title><recorditem><![CDATA[<recordinfo><datalist>]]></recorditem>", 19)
        self.assertEqual(self.p(bad, app(19))[:2], ("[聊天记录] 记录", {"title": "记录"}))

    # ---------------------------------------------------------- pat / system
    def test_pat(self):
        text, extra, kind = self.p(PAT, app(62))
        self.assertEqual((text, kind), ("[拍一拍] Alice tickled me", "pat"))
        self.assertEqual(extra, {"from": "Alice", "patted": "我"})
        self.assertEqual(self.p(PAT_TEMPLATE_ONLY, app(62))[0], '[拍一拍] "Alice" 拍了拍 "Bob"')

    def test_system(self):
        self.assertEqual(self.p(SYS_TEMPLATE, 10000)[0],
                         "[系统消息] Alice invited you and Xavier, Bob to the group chat")
        self.assertEqual(self.p(SYS_TEMPLATE, 10000, res=None)[0],
                         "[系统消息] Alice Nick invited you and Xavier, Bob Nick to the group chat")
        self.assertEqual(self.p(SYS_REVOKE, 10002)[:3:2],
                         ('[系统消息] "Alice" recalled a message', "system"))
        self.assertEqual(self.p(SYS_PAYMSG, 10000)[0], "[系统消息] 你有一笔待接收的转账")
        self.assertEqual(self.p(SYS_PAT, 10000)[0], '[拍一拍] "Alice" 拍了拍 "Bob"')
        self.assertEqual(self.p('<img src="x.png"/> Bob opened your <_wc_custom_link_ '
                                'href="weixin://x">Red Packet</_wc_custom_link_>', 10000)[0],
                         "[系统消息] Bob opened your Red Packet")
        self.assertEqual(self.p("", 10000)[0], "[系统消息]")

    # ---------------------------------------------------------- misc
    def test_location(self):
        text, extra, kind = self.p(LOCATION, 48)
        self.assertEqual((text, kind), ("[位置] 示例咖啡馆 (示例市示例路1号)", "location"))
        self.assertEqual(extra, {"poiname": "示例咖啡馆", "label": "示例市示例路1号",
                                 "lat": 31.260204, "lng": 120.747391})
        self.assertEqual(self.p(None, 48)[:2], ("[位置]", None))

    def test_simple_types_unchanged(self):
        self.assertEqual(self.p("hi", 1), ("hi", None, "text"))
        self.assertEqual(self.p("", 1)[0], None)
        for lt, label in ((3, "[图片]"), (34, "[语音]"), (43, "[视频]"), (47, "[表情]")):
            self.assertEqual(self.p("<msg/>", lt)[0], label)
        self.assertEqual(self.p(appmsg("<title>x</title>", 8), app(8))[0], "[表情]")
        self.assertIsNone(self.p("", 11000 | (17 << 32))[0])

    def test_unknown(self):
        self.assertEqual(self.p(appmsg("<title>新功能</title>", 999), app(999)),
                         ("新功能", None, "other"))
        self.assertEqual(self.p(appmsg("<title></title>", 999), app(999))[0], "[应用消息:999]")
        self.assertEqual(self.p("<msg><foo/></msg>", 12345)[0], "[消息类型:12345]")
        self.assertEqual(self.p("short text", 12345)[0], "short text")
        self.assertEqual(self.p('<msg username="u" nickname="客服" />', 67)[0], "[名片] 客服")

    def test_xml_type_wins_over_sub(self):
        # The appmsg's own <type> decides, not the first <type> in the document
        # (QUOTE_IMAGE has refermsg <type>3</type> before <type>57</type>).
        self.assertLess(QUOTE_IMAGE.index("<type>3<"), QUOTE_IMAGE.index("<type>57<"))
        self.assertEqual(chats.message_kind(app(57), QUOTE_IMAGE), "quote")
        # XML type overrides the local_type sub type.
        self.assertEqual(self.p(LINK, app(57))[2], "link")

    def test_hostile_xml(self):
        bomb = ('<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">'
                '<!ENTITY lol2 "&lol;&lol;&lol;">]><msg><appmsg><title>&lol2;</title>'
                '<type>5</type></appmsg></msg>')
        text, extra, _ = self.p(bomb, app(5))
        self.assertNotIn("lollol", text)
        for junk in ("<", "<msg><appmsg><title>x", "\x00\x01", "&&&", "<a><b></a>"):
            for lt in (app(5), app(57), app(19), app(2000), 50, 42, 48, 10000):
                P.parse(junk, lt, resolve)  # must not raise

    def test_chats_wrappers(self):
        self.assertEqual(chats.format_message(LINK, app(5)), self.p(LINK, app(5), None)[0])
        self.assertEqual(chats.parse_message(FILE, app(6))[2], "file")


if __name__ == "__main__":
    unittest.main()
