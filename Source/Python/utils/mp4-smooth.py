#!/usr/bin/env python

__author__    = 'Gilles Boccon-Gibod (bok@bok.net)'
__copyright__ = 'Copyright 2011-2013 Axiomatic Systems, LLC.'

###
# NOTE: this script needs Bento4 command line binaries to run
# You must place the 'mp4info' 'mp4dump', 'mp4encrypt' and 'mp4split' binaries
# in a directory named 'bin/<platform>' at the same level as where
# this script is.
# <platform> depends on the platform you're running on:
# Mac OSX   --> platform = macosx
# Linux x86 --> platform = linux-x86
# Windows   --> platform = win32

import sys
import os
import os.path as path
from optparse import OptionParser, make_option, OptionError
from subprocess import check_output, CalledProcessError
import urlparse
import random
import base64
import shutil
import tempfile
import json
import io
import struct
import xml.etree.ElementTree as xml
from xml.dom.minidom import parseString
import operator
import tempfile

# setup main options
SCRIPT_PATH = path.abspath(path.dirname(__file__))
sys.path += [SCRIPT_PATH]

VIDEO_MIMETYPE           = 'video/mp4'
AUDIO_MIMETYPE           = 'audio/mp4'
VIDEO_DIR                = 'video'
AUDIO_DIR                = 'audio'
SMOOTH_DEFAULT_TIMESCALE = 10000000
SMIL_NAMESPACE           = 'http://www.w3.org/2001/SMIL20/Language'
LINEAR_PATTERN           = 'media-%02d.ismv'

def PrintErrorAndExit(message):
    sys.stderr.write(message+'\n')
    sys.exit(1)
    
def XmlDuration(d):
    h  = d/3600
    d -= h*3600
    m  = d/60
    s  = d-m*60
    xsd = 'PT'
    if h:
        xsd += str(h)+'H'
    if h or m:
        xsd += str(m)+'M'
    if s:
        xsd += str(s)+'S'
    return xsd
    
def Bento4Command(name, *args, **kwargs):
    cmd = [path.join(Options.exec_dir, name)]
    for kwarg in kwargs:
        arg = kwarg.replace('_', '-')
        cmd.append('--'+arg)
        if not isinstance(kwargs[kwarg], bool):
            cmd.append(kwargs[kwarg])
    cmd += args
    #print cmd
    try:
        return check_output(cmd) 
    except CalledProcessError, e:
        #print e
        raise Exception("binary tool failed with error %d" % e.returncode)
    
def Mp4Info(filename, **args):
    return Bento4Command('mp4info', filename, **args)

def Mp4Dump(filename, **args):
    return Bento4Command('mp4dump', filename, **args)

def Mp4Split(filename, **args):
    return Bento4Command('mp4split', filename, **args)

def Mp4Encrypt(input_filename, output_filename, **args):
    return Bento4Command('mp4encrypt', input_filename, output_filename, **args)

class Mp4Atom:
    def __init__(self, type, size, position):
        self.type     = type
        self.size     = size
        self.position = position
        
def WalkAtoms(filename):
    cursor = 0
    atoms = []
    file = io.FileIO(filename, "rb")
    while True:
        try:
            size = struct.unpack('>I', file.read(4))[0]
            type = file.read(4)
            if size == 1:
                size = struct.unpack('>Q', file.read(8))[0]
            #print type,size
            atoms.append(Mp4Atom(type, size, cursor))
            cursor += size
            file.seek(cursor)
        except:
            break
        
    return atoms
        
def FilterChildren(parent, type):
    if isinstance(parent, list):
        children = parent
    else:
        children = parent['children']
    return [child for child in children if child['name'] == type]

def FindChild(top, path):
    for entry in path:
        children = FilterChildren(top, entry)
        if len(children) == 0: return None
        top = children[0]
    return top

class Mp4Track:
    def __init__(self, parent, info):
        self.parent = parent
        self.info   = info
        self.default_sample_duration  = 0
        self.timescale                = 0
        self.moofs                    = []
        self.kid                      = None
        self.sample_counts            = []
        self.segment_sizes            = []
        self.segment_durations        = []
        self.segment_scaled_durations = []
        self.total_sample_count       = 0
        self.total_duration           = 0
        self.average_segment_duration = 0
        self.average_segment_bitrate  = 0
        self.max_segment_bitrate      = 0
        self.id = info['id']
        if info['type'] == 'Audio':
            self.type = 'audio'
        elif info['type'] == 'Video':
            self.type = 'video'
        else:
            self.type = 'other'
        
        sample_desc = info['sample_descriptions'][0]
        if self.type == 'video':
            # get the width and height
            self.width  = sample_desc['width']
            self.height = sample_desc['height']

        if self.type == 'audio':
            self.sample_rate = sample_desc['sample_rate']
            self.channels = sample_desc['channels']
                        
    def update(self):
        # compute the total number of samples
        self.total_sample_count = reduce(operator.add, self.sample_counts, 0)
        
        # compute the total duration
        self.total_duration = reduce(operator.add, self.segment_durations, 0)
        
        # compute the average segment durations
        segment_count = len(self.segment_durations)
        if segment_count >= 1:
            # do not count the last segment, which could be shorter
            self.average_segment_duration = reduce(operator.add, self.segment_durations[:-1], 0)/float(segment_count-1)
        elif segment_count == 1:
            self.average_segment_duration = self.segment_durations[0]
    
        # compute the average segment bitrates
        self.media_size = reduce(operator.add, self.segment_sizes, 0)
        if self.total_duration:
            self.average_segment_bitrate = int(8.0*float(self.media_size)/self.total_duration)

        # compute the max segment bitrates
        if self.average_segment_duration:
            self.max_segment_bitrate = 8*int(float(max(self.segment_sizes[:-1]))/self.average_segment_duration)

    def compute_kid(self):
        moov = FilterChildren(self.parent.tree, 'moov')[0]
        traks = FilterChildren(moov, 'trak')
        for trak in traks:
            tkhd = FindChild(trak, ['tkhd'])
            track_id = tkhd['id']
            tenc = FindChild(trak, ('mdia', 'minf', 'stbl', 'stsd', 'encv', 'sinf', 'schi', 'tenc'))
            if tenc is None:
                tenc = FindChild(trak, ('mdia', 'minf', 'stbl', 'stsd', 'enca', 'sinf', 'schi', 'tenc'))
            if tenc and 'default_KID' in tenc:
                kid = tenc['default_KID'].strip('[]').replace(' ', '')
                self.kid = kid
    
    def __repr__(self):
        return 'File '+str(self.parent.index)+'#'+str(self.id)
    
class Mp4File:
    def __init__(self, filename):
        self.filename = filename
        self.tracks   = {}
                
        if Options.debug: print 'Processing MP4 file', filename

        # walk the atom structure
        self.atoms = WalkAtoms(filename)
        self.segments = []
        for atom in self.atoms:
            if atom.type == 'moov':
                self.init_segment = atom
            elif atom.type == 'moof':
                self.segments.append([atom])
            else:
                if len(self.segments):
                    self.segments[-1].append(atom)
        #print self.segments
        if Options.debug: print '  found', len(self.segments), 'segments'
                        
        # get the mp4 file info
        json_info = Mp4Info(filename, format='json')
        self.info = json.loads(json_info, strict=False)

        for track in self.info['tracks']:
            self.tracks[track['id']] = Mp4Track(self, track)

        # get a complete file dump
        json_dump = Mp4Dump(filename, format='json', verbosity='1')
        #print json_dump
        self.tree = json.loads(json_dump, strict=False)
        
        # look for KIDs
        for track in self.tracks.itervalues():
            track.compute_kid()
                
        # compute default sample durations and timescales
        for atom in self.tree:
            if atom['name'] == 'moov':
                for c1 in atom['children']:
                    if c1['name'] == 'mvex':
                        for c2 in c1['children']:
                            if c2['name'] == 'trex':
                                self.tracks[c2['track id']].default_sample_duration = c2['default sample duration']
                    elif c1['name'] == 'trak':
                        track_id = 0
                        for c2 in c1['children']:
                            if c2['name'] == 'tkhd':
                                track_id = c2['id']
                        for c2 in c1['children']:
                            if c2['name'] == 'mdia':
                                for c3 in c2['children']:
                                    if c3['name'] == 'mdhd':
                                        self.tracks[track_id].timescale = c3['timescale']

        # partition the segments
        segment_index = 0
        track = None
        for atom in self.tree:
            if atom['name'] == 'moof':
                trafs = FilterChildren(atom, 'traf')
                if len(trafs) != 1:
                    PrintErrorAndExit('ERROR: unsupported input file, more than one "traf" box in fragment')
                tfhd = FilterChildren(trafs[0], 'tfhd')[0]
                track = self.tracks[tfhd['track ID']]
                track.moofs.append(segment_index)
                track.segment_sizes.append(0)
                segment_duration = 0
                for trun in FilterChildren(trafs[0], 'trun'):
                    track.sample_counts.append(trun['sample count'])
                    for (name, value) in trun.items():
                        if name.startswith('entry '):
                            sample_duration = track.default_sample_duration
                            f = value.find('duration:')
                            if f >= 0:
                                f += 9
                                g = value.find(' ', f)
                                if g >= 0:
                                    sample_duration = int(value[f:g])    
                            segment_duration += sample_duration
                track.segment_scaled_durations.append(segment_duration)
                track.segment_durations.append(float(segment_duration)/float(track.timescale))
                segment_index += 1
            else:
                if track and len(track.segment_sizes):
                    track.segment_sizes[-1] += atom['size']
                                                
        # compute the total numer of samples for each track
        for track_id in self.tracks:
            self.tracks[track_id].update()
                                                   
        # print debug info if requested
        if Options.debug:
            for track in self.tracks.itervalues():
                print '    ID                       =', track.id
                print '    Type                     =', track.type
                print '    Sample Count             =', track.total_sample_count
                print '    Average segment bitrate  =', track.average_segment_bitrate
                print '    Max segment bitrate      =', track.average_segment_bitrate
                print '    Average segment duration =', track.average_segment_duration

    def find_track_by_id(self, track_id_to_find):
        for track_id in self.tracks:
            if track_id_to_find == 0 or track_id_to_find == track_id:
                return self.tracks[track_id]
        
        return None

    def find_track_by_type(self, track_type_to_find):
        for track_id in self.tracks:
            if track_type_to_find == '' or track_type_to_find == self.tracks[track_id].type:
                return self.tracks[track_id]
        
        return None
            
class MediaSource:
    def __init__(self, name):
        self.name = name
        if name.startswith('[') and ']' in name:
            try:
                params = name[1:name.find(']')]
                self.filename = name[2+len(params):]
                self.spec = dict([x.split('=') for x in params.split(',')])
                for int_param in ['track']:
                    if int_param in self.spec: self.spec[int_param] = int(self.spec[int_param])
            except:
                raise Exception('Invalid syntax for media file spec "'+name+'"')
        else:
            self.filename = name
            self.spec = {}
            
        if 'type'     not in self.spec: self.spec['type']     = ''
        if 'track'    not in self.spec: self.spec['track']    = 0
        if 'language' not in self.spec: self.spec['language'] = ''
        
    def __repr__(self):
        return self.name

def MakeNewDir(dir, is_warning=False):
    if os.path.exists(dir):
        if is_warning:
            sys.stderr.write('WARNING: ')
        else:
            sys.stderr.write('ERROR: ')
        sys.stderr.write('directory "'+dir+'" already exists\n')
        if not is_warning:
            sys.exit(1)
    else:
        os.mkdir(dir)
        
Options = None            
def main():
    # determine the platform binary name
    platform = sys.platform
    if platform.startswith('linux'):
        platform = 'linux-x86'
    elif platform.startswith('darwin'):
        platform = 'macosx'
                
    # parse options
    parser = OptionParser(usage="%prog [options] <media-file> [<media-file> ...]")
    parser.add_option('', '--verbose', dest="verbose",
                      action='store_true', default=False,
                      help="Be verbose")
    parser.add_option('', '--debug', dest="debug",
                      action='store_true', default=False,
                      help="Print out debugging information")
    parser.add_option('-o', '--output-dir', dest="output_dir",
                      help="Output directory", metavar="<output-dir>", default='output')
    parser.add_option('-m', '--client-manifest-name', dest="client_manifest_filename",
                      help="Client Manifest file name", metavar="<filename>", default='stream.ismc')
    parser.add_option('-s', '--server-manifest-name', dest="server_manifest_filename",
                      help="Server Manifest file name", metavar="<filename>", default='stream.ism')
    parser.add_option('', '--no-media', dest="no_media",
                      action='store_true', default=False,
                      help="Do not output media files")
    parser.add_option('', "--split",
                      action="store_true", dest="split", default=False,
                      help="Split the file into segments")
    parser.add_option('', "--encryption-key", dest="encryption_key", metavar='<KID>:<key>', default=None,
                      help="Encrypt all audio and video tracks with AES key <key> (in hex) with KID <KID> (in hex)")
    parser.add_option('', "--playready",
                      dest="playready", action="store_true", default=False,
                      help="Add PlayReady signaling to the client Manifest (requires an encrypted input, or the --encryption-key option)")
    parser.add_option('', "--exec-dir", metavar="<exec_dir>",
                      dest="exec_dir", default=path.join(SCRIPT_PATH, 'bin', platform),
                      help="Directory where the Bento4 executables are located")
    (options, args) = parser.parse_args()
    if len(args) == 0:
        parser.print_help()
        sys.exit(1)
    global Options
    Options = options
    
    # parse media sources syntax
    media_sources = [MediaSource(source) for source in args]
    
    # check the consistency of the options
    if not path.exists(Options.exec_dir):
        PrintErrorAndExit('Executable directory does not exist ('+Options.exec_dir+'), use --exec-dir')

    # create the output directory
    MakeNewDir(options.output_dir, is_warning=options.no_media)

    # keep track of media file names (in case we use temporary files when encrypting)
    file_name_map = {}
    for media_source in media_sources:
        file_name_map[media_source.filename] = media_source.filename

    # encrypt the input files if needed
    encrypted_files = []
    if options.encryption_key:
        if ':' not in options.encryption_key:
            raise Exception('Invalid argument syntax for --encryption-key option')
        kid_b64, key_b64 = options.encryption_key.split(':')
        if len(kid_b64) != 32 or len(key_b64) != 32:
            raise Exception('Invalid argument format for --encryption-key option')
            
        track_ids = []
        for media_source in media_sources:
            media_file = media_source.filename
            # get the mp4 file info
            json_info = Mp4Info(media_file, format='json')
            info = json.loads(json_info, strict=False)

            track_ids = [track['id'] for track in info['tracks'] if track['type'] in ['Audio', 'Video']]
            print 'Encrypting track IDs '+str(track_ids)+' in '+ media_file
            encrypted_file = tempfile.NamedTemporaryFile(dir = options.output_dir)
            encrypted_files.append(encrypted_file) # keep a ref so that it does not get deleted
            file_name_map[encrypted_file.name] = encrypted_file.name + ' (Encrypted ' + media_file + ')'
            args = ['--method', 'MPEG-CENC']
            args.append(media_file)
            args.append(encrypted_file.name)
            for track_id in track_ids:
                args += ['--key', str(track_id)+':'+key_b64+':random', '--property', str(track_id)+':KID:'+kid_b64] 
            cmd = [path.join(Options.exec_dir, 'mp4encrypt')] + args
            try:
                check_output(cmd) 
            except CalledProcessError, e:
                raise Exception("binary tool failed with error %d" % e.returncode)
            media_source.filename = encrypted_file.name
            
    # parse the media files
    index = 1
    for media_source in media_sources:
        media_file = media_source.filename
        print 'Parsing media file', str(index)+':', file_name_map[media_file]
        if not os.path.exists(media_file):
            PrintErrorAndExit('ERROR: media file ' + media_file + ' does not exist')
            
        # get the file info
        mp4_file = Mp4File(media_file)
        mp4_file.index = index
        
        # check the file
        if mp4_file.info['movie']['fragments'] != True:
            PrintErrorAndExit('ERROR: file '+str(mp4_file.index)+' is not fragmented (use mp4fragment to fragment it)')
            
        # add the file to the file
        media_source.mp4_file = mp4_file
        
        index += 1
        
    # select the audio and video tracks
    audio_tracks = {}
    video_tracks = []
    for media_source in media_sources:
        track_id   = media_source.spec['track']
        track_type = media_source.spec['type']
        track      = None
        
        if track_type not in ['', 'audio', 'video']:
            sys.stderr.write('WARNING: ignoring source '+media_source.name+', unknown type')

        if track_id:
            track = media_source.mp4_file.find_track_by_id(track_id)
            if not track:
                PrintErrorAndExit('ERROR: track id not found for media file '+media_source.name)

        if track and track_type and track.type != track_type:
            PrintErrorAndExit('ERROR: track type mismatch for media file '+media_source.name)

        audio_track = track
        if track_type == 'audio' or track_type == '':
            if audio_track is None:
                audio_track = media_source.mp4_file.find_track_by_type('audio')
            if audio_track:
                language = media_source.spec['language']
                if language not in audio_tracks:
                    audio_tracks[language] = audio_track
            else:
                if track_type:
                    sys.stderr.write('WARNING: no audio track found in '+media_source.name+'\n')
                    
        # audio tracks with languages don't mix with language-less tracks
        if len(audio_tracks) > 1 and '' in audio_tracks:
            del audio_tracks['']
            
        video_track = track
        if track_type == 'video' or track_type == '':
            if video_track is None:
                video_track = media_source.mp4_file.find_track_by_type('video')
            if video_track:
                video_tracks.append(video_track)
            else:
                if track_type:
                    sys.stderr.write('WARNING: no video track found in '+media_source.name+'\n')
        
    # check that we have at least one audio and one video
    if len(audio_tracks) == 0:
        PrintErrorAndExit('ERROR: no audio track selected')
    if len(video_tracks) == 0:
        PrintErrorAndExit('ERROR: no video track selected')
        
    if Options.verbose:
        print 'Audio:', audio_tracks
        print 'Video:', video_tracks
        
    # check that segments are consistent between files
    prev_track = None
    for track in video_tracks:
        if prev_track:
            if track.total_sample_count != prev_track.total_sample_count:
                sys.stderr.write('WARNING: video sample count mismatch between "'+str(track)+'" and "'+str(prev_track)+'"\n')
        prev_track = track
        
    # check that the video segments match
    for track in video_tracks:
        if track.sample_counts[:-1] != video_tracks[0].sample_counts[:-1]:
            PrintErrorAndExit('ERROR: video tracks are not aligned ("'+str(track)+'" differs)')
               
    # check that the video segment durations are almost all equal
    for video_track in video_tracks:
        for segment_duration in video_track.segment_durations[:-1]:
            ratio = segment_duration/video_track.average_segment_duration
            if ratio > 1.1 or ratio < 0.9:
                sys.stderr.write('WARNING: video segment durations for "' + str(video_track) + '" vary by more than 10%\n')
                break;
    for audio_track in audio_tracks.values():
        for segment_duration in audio_track.segment_durations[:-1]:
            ratio = segment_duration/audio_track.average_segment_duration
            if ratio > 1.1 or ratio < 0.9:
                sys.stderr.write('WARNING: audio segment durations for "' + str(audio_track) + '" vary by more than 10%\n')
                break;
                    
    # compute the total duration (we take the duration of the video)
    presentation_duration = int(float(SMOOTH_DEFAULT_TIMESCALE)*video_tracks[0].total_duration)
        
    # create the Client Manifest
    client_manifest = xml.Element('SmoothStreamingMedia', 
                                  MajorVersion="2", 
                                  MinorVersion="0",
                                  Duration=str(presentation_duration))
    client_manifest.append(xml.Comment('Created with Bento4 mp4-smooth.py'))
    
    # process the audio tracks
    audio_index = 0
    for (language, audio_track) in audio_tracks.iteritems():
        if language:
            id_ext = "."+language
            stream_name = "audio_"+language
        else:
            id_ext = ''
            stream_name = "audio"
        audio_url_pattern="QualityLevels({bitrate})/Fragments(%s={start time})" % (stream_name)
        stream_index = xml.SubElement(client_manifest, 
                                      'StreamIndex', 
                                      Chunks=str(len(track.moofs)), 
                                      Url=audio_url_pattern, 
                                      Type="audio", 
                                      Name=stream_name, 
                                      QualityLevels="1",
                                      TimeScale=str(audio_track.timescale))
        if language:
            stream_index.set('Language', language)
        bandwidth = audio_track.max_segment_bitrate
        quality_level = xml.SubElement(stream_index, 
                                       'QualityLevel', 
                                       Bitrate=str(bandwidth), 
                                       SamplingRate=str(audio_track.sample_rate),
                                       Channels=str(audio_track.channels), 
                                       BitsPerSample="16", 
                                       PacketSize="4", 
                                       AudioTag="255", 
                                       FourCC="AACL",
                                       Index="0",
                                       CodecPrivateData=audio_track.info['sample_descriptions'][0]['decoder_info'])

        for duration in audio_track.segment_scaled_durations:
            xml.SubElement(stream_index, "c", d=str(duration))
        
    # process all the video tracks
    max_width  = max([track.width  for track in video_tracks])
    max_height = max([track.height for track in video_tracks])
    video_url_pattern="QualityLevels({bitrate})/Fragments(video={start time})"
    stream_index = xml.SubElement(client_manifest, 
                                  'StreamIndex',
                                   Chunks=str(len(video_tracks[0].moofs)), 
                                   Url=video_url_pattern, 
                                   Type="video", 
                                   Name="video", 
                                   QualityLevels=str(len(video_tracks)),
                                   TimeScale=str(video_tracks[0].timescale),
                                   MaxWidth=str(max_width),
                                   MaxHeight=str(max_height))
    qindex = 0
    for video_track in video_tracks:
        bandwidth = video_track.max_segment_bitrate
        sample_desc = video_track.info['sample_descriptions'][0]
        codec_private_data = '00000001'+sample_desc['avc_sps'][0]+'00000001'+sample_desc['avc_pps'][0]
        quality_level = xml.SubElement(stream_index, 
                                       'QualityLevel', 
                                       Bitrate=str(bandwidth),
                                       MaxWidth=str(video_track.width), 
                                       MaxHeight=str(video_track.height),
                                       FourCC="H264",
                                       CodecPrivateData=codec_private_data,
                                       Index=str(qindex))
        qindex += 1

    for duration in video_tracks[0].segment_scaled_durations:
        xml.SubElement(stream_index, "c", d=str(duration))
    
    if options.verbose:
        for audio_track in audio_tracks.itervalues():
            print '  Audio Track: '+str(audio_track)+' - max bitrate=%d, avg bitrate=%d' % (audio_track.max_segment_bitrate, audio_track.average_segment_bitrate)
        for video_track in video_tracks:
            print '  Video Track: '+str(video_track)+' - max bitrate=%d, avg bitrate=%d' % (video_track.max_segment_bitrate, video_track.average_segment_bitrate)
            
    # save the Client Manifest
    if options.client_manifest_filename != '':
        open(path.join(options.output_dir, options.client_manifest_filename), "wb").write(parseString(xml.tostring(client_manifest)).toprettyxml("  "))
        

    # create the Server Manifest file
    server_manifest = xml.Element('smil', xmlns=SMIL_NAMESPACE)
    server_manifest_head = xml.SubElement(server_manifest , 'head')
    xml.SubElement(server_manifest_head, 'meta', name='clientManifestRelativePath', content=path.basename(options.client_manifest_filename))
    server_manifest_body = xml.SubElement(server_manifest , 'body')
    server_manifest_switch = xml.SubElement(server_manifest_body, 'switch')
    for (language, audio_track) in audio_tracks.iteritems():
        audio_entry = xml.SubElement(server_manifest_switch, 'audio', src=LINEAR_PATTERN%(audio_track.parent.index), systemBitrate=str(audio_track.max_segment_bitrate))
        xml.SubElement(audio_entry, 'param', name='trackID', value=str(audio_track.id), valueType='data')
        if language:
            xml.SubElement(audio_entry, 'param', name='trackName', value="audio_"+language, valueType='data')
        if audio_track.timescale != SMOOTH_DEFAULT_TIMESCALE:
            xml.SubElement(audio_entry, 'param', name='timeScale', value=str(audio_track.timescale), valueType='data')
        
    for video_track in video_tracks:
        video_entry = xml.SubElement(server_manifest_switch, 'video', src=LINEAR_PATTERN%(video_track.parent.index), systemBitrate=str(video_track.max_segment_bitrate))
        xml.SubElement(video_entry, 'param', name='trackID', value=str(video_track.id), valueType='data')
        if video_track.timescale != SMOOTH_DEFAULT_TIMESCALE:
            xml.SubElement(video_entry, 'param', name='timeScale', value=str(video_track.timescale), valueType='data')
    
    # save the Manifest
    if options.server_manifest_filename != '':
        open(path.join(options.output_dir, options.server_manifest_filename), "wb").write(parseString(xml.tostring(server_manifest)).toprettyxml("  "))
    
    # copy the media files
    if not options.no_media:
        if options.split:
            MakeNewDir(path.join(options.output_dir, 'audio'))
            for (language, audio_track) in audio_tracks.iteritems():
                out_dir = path.join(options.output_dir, 'audio')
                if len(audio_tracks) > 1:
                    out_dir = path.join(out_dir, language)
                    MakeNewDir(out_dir)
                print 'Processing media file (audio)', file_name_map[audio_track.parent.filename]
                Mp4Split(audio_track.parent.filename,
                         track_id               = str(audio_track.id),
                         no_track_id_in_pattern = True,
                         init_segment           = path.join(out_dir, INIT_SEGMENT_NAME),
                         media_segment          = path.join(out_dir, SEGMENT_PATTERN))
        
            MakeNewDir(path.join(options.output_dir, 'video'))
            for video_track in video_tracks:
                out_dir = path.join(options.output_dir, 'video', str(video_track.parent.index))
                MakeNewDir(out_dir)
                print 'Processing media file (video)', file_name_map[video_track.parent.filename]
                Mp4Split(video_track.parent.filename,
                         track_id               = str(video_track.id),
                         no_track_id_in_pattern = True,
                         init_segment           = path.join(out_dir, INIT_SEGMENT_NAME),
                         media_segment          = path.join(out_dir, SEGMENT_PATTERN))
        else:
            for media_source in media_sources:
                print 'Processing media file', file_name_map[media_source.mp4_file.filename]
                shutil.copyfile(media_source.mp4_file.filename,
                                path.join(options.output_dir, LINEAR_PATTERN%(media_source.mp4_file.index)))


###########################    
if __name__ == '__main__':
    try:
        main()
    except Exception, err:
        if Options.debug:
            raise
        else:
            PrintErrorAndExit('ERROR: %s\n' % str(err))