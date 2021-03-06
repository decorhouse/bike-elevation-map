import argparse
import datetime
import json
import logging
import os.path
import sys
import time

import pickle

import imp

import requests

############
# timer util
############
def timeit(func):
    def timed(*args, **kwargs):
        start = time.time()
        result = func(*args, **kwargs)
        end = time.time()

        print '%r %2.2f sec' % \
              (func.__name__, end-start)
        return result
    return timed

############
# data definition getters and intersection generation
############
@timeit
def compute_all_intersections(source_file, cache=None):
    """
    Given a source file to draw paths from, return a set of all intersections
    among those paths.
    """
    all_intersections = set([])

    assert os.path.exists(source_file)
    data_module = imp.load_source('local_data', source_file)

    # TODO: use getattr, since it is easier to distinguish strings
    for region in data_module.regions:
        while len(region) > 0:
            bucket = region.pop(0)
            for street in bucket:
                for other_bucket in region:
                    for other_street in other_bucket:
                        all_intersections.add('%s and %s' % (street, other_street))

    return all_intersections

def get_all_paths(source_file):
    """
    Given a source file to draw paths from, get all the paths.
    """
    all_paths = set([])

    assert os.path.exists(source_file)
    data_module = imp.load_source('local_data', source_file)

    for region in data_module.regions:
        while len(region) > 0:
            bucket = region.pop(0)
            for street in bucket:
                all_paths.add(street)

    return all_paths

def get_path_breaks(source_file, path):
    """
    Given a source file and a path, get the breaks on that path.
    """
    data_module = imp.load_source('local_data', source_file)
    return data_module.breaks.get(path, set([]))

def get_curved_roads(source_file):
    """
    Given a source file, get the dict of curved roads.
    """
    data_module = imp.load_source('local_data', source_file)
    return data_module.curved_roads

def get_custom_paths(source_file):
    """
    Given a source file, get the dict of custom paths.
    """
    data_module = imp.load_source('local_data', source_file)
    return data_module.custom_paths

def get_route_directives(source_file):
    """
    Given a source file, get the dict of route directives.
    """
    data_module = imp.load_source('local_data', source_file)
    return data_module.route_directives

def get_city(source_file):
    """
    Given a source file, get the city it refers to.
    """
    data_module = imp.load_source('local_data', source_file)
    return data_module.city

def get_tbds(source_file):
    """
    Given a source file, get the TBD markers we need.
    """
    data_module = imp.load_source('local_data', source_file)
    return data_module.tbds

####################
# bad address cache functions
####################
BAD_CACHE_ATTRS = ['not_intersection', 'ambiguous']
BAD_CACHE_ATTR_DELIMITER = '--- '

def create_empty_bad_address_cache():
    return {attr: set([]) for attr in BAD_CACHE_ATTRS}

def load_bad_address_cache(fp):
    cache = create_empty_bad_address_cache()
    current_attr = None
    for line in fp:
        stripped_line = line.strip()
        if stripped_line == '':
            continue
        elif stripped_line.startswith(BAD_CACHE_ATTR_DELIMITER):
            current_attr = stripped_line[len(BAD_CACHE_ATTR_DELIMITER):]
        else:
            cache[current_attr].add(stripped_line)
    return cache

def write_bad_address_cache(fp, cache):
    for attr in BAD_CACHE_ATTRS:
        fp.write('%s%s\n' % (BAD_CACHE_ATTR_DELIMITER, attr))
        for bad_address in cache[attr]:
            fp.write(bad_address)
            fp.write('\n')


##################
# google api calls
##################
def get_lat_lng_and_elevation(intersection, city, custom=False):
    """
    Given an intersection string ("Divisadero St and McAllister St"),
    and a city string ("San Francisco, CA"), return the latitude,
    longitude, and elevation as a tuple, or raise an exception.
    """
    lat, lng = get_geocode(intersection, city, custom=custom)
    elevation = get_elevation(lat, lng)
    return lat, lng, elevation


def get_geocode(intersection, city, custom=False):
    """
    Given an intersection string ("Divisadero St and McAllister St"),
    and a city string ("San Francisco, CA"), return the latitude
    and longitude as a tuple, or raise an exception.
    """
    original_city = city
    city = city.replace(' ', '+')

    geocode_uri = 'http://maps.googleapis.com/maps/api/geocode/json?address=%s,+%s&sensor=false' % (intersection, city)

    data = make_json_request(geocode_uri)

    if len(data['results']) > 1:
        logging.warning('Got more than one result for geocode uri...: %s' % geocode_uri)
        raise AmbiguousAddressException(intersection, city)

    parts = intersection.split(' and ')

    short_parts = data['results'][0]['address_components']

    # Verify the address came back clean
    # XXX: this is hacky - we should really do this by word splitting
    translations = {
        ' Blvd': ' Boulevard',
        ' Dr': ' Drive',
        ' Rd': ' Road',
        ' St': ' Street',
        ' Blvd': ' Boulevard',
        ' Ave': ' Avenue',
    }
    def translate_address(address, t_dict):
        for entry, trans in t_dict.iteritems():
            address = address.replace(entry, trans)
        return address

    formatted_addr = data['results'][0]['formatted_address']
    # XXX: super hacky! to deal with addresses like 360 John F Kennedy Dr.
    if len(parts) < 2 and custom:
        parts = [parts[0], '']

    if (parts[0] not in formatted_addr and translate_address(parts[0], translations) not in formatted_addr) or \
       (parts[1] not in formatted_addr and translate_address(parts[1], translations) not in formatted_addr) or \
       original_city not in formatted_addr:
        logging.error('This address was not an intersection!: %s' % geocode_uri)
        raise NotIntersectionAddressException(intersection, city)


    #if 'intersection' not in data['results'][0]['types']:
    #   logging.error('This address was not an intersection!: %s' % geocode_uri)
    #   raise NotIntersectionAddressException(intersection, city)

    latitude = data['results'][0]['geometry']['location']['lat']
    longitude = data['results'][0]['geometry']['location']['lng']

    return latitude, longitude


def get_elevation(lat, lng):
    """
    Given a latitude and a longitude, return the elevation at the point, in meters.
    """
    elevation_uri = 'http://maps.googleapis.com/maps/api/elevation/json?locations=%s,%s&sensor=false' % (lat, lng)
    data = make_json_request(elevation_uri)
    return data['results'][0]['elevation']


def get_directions_and_length(origin, destination, city):
    """
    Given an origin and destination, return the
    encoded directions path, as well as the distance of the trip,
    in a dict.
    """
    directions_uri = 'http://maps.googleapis.com/maps/api/directions/json?origin=%s&destination=%s&sensor=false&mode=walking' % \
                        (origin.replace(' ', '+') + ', ' + city, destination.replace(' ', '+') + ', ' + city)
    data = make_json_request(directions_uri)
    print directions_uri

    try:
        if len(data['routes']) > 1:
            logging.warning('> 1 route on directions req: %s' % directions_uri)
        # Note: we are doing point to point directions, so there will only be
        # one "leg" of the journey from the Google Directions API
        return {'path': data['routes'][0]['overview_polyline']['points'], 'length': data['routes'][0]['legs'][0]['distance']['value']}
    except:
        logging.error('failed directions request: %s' % directions_uri)
        pass


def make_json_request(uri):
    """
    Given an URL, return the content at the URL in JSON.

    Raises Exception if status code is not 200.
    """
    req = requests.get(uri)
    if req.status_code != 200:
        logging.error('failed request: %s' % uri)
        raise GoogleMapsApiException(uri, req.status_code)
    return json.loads(req.content)


#################
# exceptions
#################
class GoogleMapsApiException(Exception):
    def __init__(self, *args):
        self.args = args
    def __str__(self):
        return repr(self.args)

class NotIntersectionAddressException(GoogleMapsApiException):
    pass

class AmbiguousAddressException(GoogleMapsApiException):
    pass


##############
# main functions
##############
@timeit
def lookup_all_intersections(cache, intersections, bad_address_cache, city):
    """
    Fill the caches with stuff.
    """
    stats = {key: 0 for key in ['good', 'cached', 'skipped', 'bad', 'error']}

    i_cache = cache['intersections']
    p_cache = cache['paths']

    for intersection in intersections:
        parts = intersection.split(' and ')
        flip_intersection = parts[1] + ' and ' + parts[0]
        if intersection in bad_address_cache['not_intersection'] or intersection in bad_address_cache['ambiguous']:
            logging.info(' [skipped] %s' % intersection)
            stats['skipped'] += 1
            continue

        if intersection in i_cache or flip_intersection in i_cache:
            logging.info(' [cached] %s' % intersection)
            stats['cached'] += 1
            continue

        try:
            latitude, longitude, elevation = get_lat_lng_and_elevation(intersection, city)
            stats['good'] += 1
        except NotIntersectionAddressException:
            bad_address_cache['not_intersection'].add(intersection)
            stats['bad'] += 1
            continue
        except AmbiguousAddressException:
            bad_address_cache['ambiguous'].add(intersection)
            stats['bad'] += 1
            continue
        except Exception as e:
            logging.error(e)
            stats['error'] += 1
            continue

        i_cache[intersection] = {'lat': latitude,
                                'lng': longitude,
                                'elevation': elevation,
                               }

        logging.info(' [fetched] %s  %s' % \
            (intersection, str(i_cache[intersection])))

        print ' [fetched] %s  %s' % (intersection, str(i_cache[intersection]))

        # If we in fact added this new intersection, add it to the paths list. We'll sort later.
        parts = intersection.split(' and ')
        for index in (0, 1):
            if parts[index] not in p_cache:
                p_cache[parts[index]] = [intersection]
            else:
                p_cache[parts[index]].append(intersection)

    return cache, bad_address_cache, stats

@timeit
def sort_path_cache(cache, input_data):
    """
    Do cool stuff

    # Now, sort the path cache, so that all paths' intersections go from west -> east or south -> south.
    # TODO: Persist, somehow, exceptions (i.e. 2nd Ave -> Fulton to Lincoln are actually two paths)

    # Here, we need to compute the minimum and maximum lats / longitudes of all intersections
    # of a street.

    # We use these to compute a reasonable starting point:
    #   - use choice = latitude
    #       if abs(max_lat - min_lat) > abs(max_lng - min_lng),
    #       else choice = lng
    #   - pick point with the minimum choice

    # To order the intersections, sort by the choice.
    # (alternatively [much more work] compute euclidean distance for each point and sort)
    # NOW DO THE SORT PER STREET

    """
    i_cache = cache['intersections']
    p_cache = cache['paths']
    cp_cache = cache['custom_path_names']

    for path in p_cache:
        # do NOT sort custom paths
        if path in cp_cache:
            continue

        #print path,

        # kill all the control data in the cache - we will recompute these every time.
        # i.e. --BREAK, etc.
        p_cache[path] = filter(lambda k: not k.startswith('--'), p_cache[path])

        # just make sure everything is unique...
        try:
            assert len(p_cache[path]) == len(set(p_cache[path]))
        except AssertionError:
            print p_cache[path]
            p_cache[path] = list(set(p_cache[path]))

        # compute the min and max lats
        min_lat = i_cache[min(p_cache[path], key=lambda k: i_cache[k]['lat'])]['lat']
        max_lat = i_cache[max(p_cache[path], key=lambda k: i_cache[k]['lat'])]['lat']
        min_lng = i_cache[min(p_cache[path], key=lambda k: i_cache[k]['lng'])]['lng']
        max_lng = i_cache[max(p_cache[path], key=lambda k: i_cache[k]['lng'])]['lng']

        #print min_lat, min_lng, max_lat, max_lng

        if abs(max_lng - min_lng) > abs(max_lat - min_lat):
            choice = 'lng'
        else:
            choice = 'lat'

        # use sorted() for creating new object, sort() for inplace.
        sorted_path = sorted(p_cache[path], key=lambda k: i_cache[k][choice])

        #if choice == 'lat':
            #print 'south -> north'
        #if choice == 'lng':
            #print 'west -> east'

        # Add the breaks!
        breaks = get_path_breaks(input_data, path)
        path_with_breaks = []
        for intersection in sorted_path:
            path_with_breaks.append(intersection)
            parts = intersection.split(' and ')
            if parts[0] in breaks or parts[1] in breaks:
                path_with_breaks.append('--BREAK')

        #print path_with_breaks

        p_cache[path] = path_with_breaks

    return cache

@timeit
def lookup_curved_road_directions(cache, input_data, city):
    p_cache = cache['paths']
    d_cache = cache['directions']
    curved_roads = get_curved_roads(input_data)

    for road, curved_sections in curved_roads.iteritems():
        in_section = False
        last_intersection = None
        for intersection in p_cache[road]:
            if len(curved_sections) == 0:
                break

            secondary_street = curved_sections[0][0] if not in_section else curved_sections[0][1]
            int_name1 = ' and '.join([road, secondary_street])
            int_name2 = ' and '.join([secondary_street, road])

            # Start the section if needed. We will call direction API on the NEXT intersection.
            if not in_section and (int_name1 == intersection or int_name2 == intersection):
                in_section = True

            # Call the direction API.
            elif in_section:
                key_name = '%s | %s' % (last_intersection, intersection)
                if key_name in d_cache:
                    logging.info(' [skipped directions] %s -> %s' % (last_intersection, intersection))
                else:
                    d_cache[key_name] = get_directions_and_length(last_intersection, intersection, city)
                    print ' [fetched directions] %s -> %s' % (last_intersection, intersection)

                # Are we done with this section?
                if int_name1 == intersection or int_name2 == intersection:
                    in_section = False
                    curved_sections.pop(0)

            last_intersection = intersection

    return cache

@timeit
def lookup_and_add_custom_paths(cache, input_data, city):
    i_cache = cache['intersections']
    p_cache = cache['paths']
    d_cache = cache['directions']
    cp_cache = cache['custom_path_names']

    custom_paths = get_custom_paths(input_data)

    for custom_path, entry in custom_paths.iteritems():
        intersections = entry['path']
        cache['custom_path_names'].append(custom_path)
        p_cache[custom_path] = []

        for intersection in intersections:
            # look it up if it, or its flip, is not there already
            parts = intersection.split(' and ')
            # ONLY FOR CUSTOM PATHS - we can do without flips
            if len(parts) < 2:
                flipped_intersection = intersection
            else:
                flipped_intersection = parts[1] + ' and ' + parts[0]
            if intersection not in i_cache and flipped_intersection not in i_cache:
                latitude, longitude, elevation = get_lat_lng_and_elevation(intersection, city, custom=True)
                # TODO: error handling similar to lookup_all_intersections
                i_cache[intersection] = {'lat': latitude,
                                        'lng': longitude,
                                        'elevation': elevation}

                # if one street matches, add it to the pcache to be sorted
                for part in parts:
                    if part in p_cache:
                        p_cache[part].append(intersection)

                print ' [fetched] %s  %s' % (intersection, str(i_cache[intersection]))
                p_cache[custom_path].append(intersection)
            else:
                logging.info(' [cached custom intersection] %s' % intersection)
                # if the intersection or its flip is already cached, make sure
                # we add the cached one to the path.
                if intersection in i_cache:
                    p_cache[custom_path].append(intersection)
                elif flipped_intersection in i_cache:
                    p_cache[custom_path].append(flipped_intersection)

        # get directions for all the intersections
        # note that we are used the possibly flipped intersections in p_cache
        last_intersection = None
        for intersection in p_cache[custom_path]:
            if last_intersection is None:
                last_intersection = intersection
                continue

            key_name = '%s | %s' % (last_intersection, intersection)
            if key_name in d_cache:
                logging.info(' [skipped custom directions] %s -> %s' % (last_intersection, intersection))
            else:
                d_cache[key_name] = get_directions_and_length(last_intersection, intersection, city)
                print ' [fetched custom directions] %s -> %s' % (last_intersection, intersection)

            last_intersection = intersection

    return cache

@timeit
def define_route_directives(cache, input_data):
    p_cache = cache['paths']
    # start from scratch every time.
    rd_cache = {}

    route_directives = get_route_directives(input_data)

    for route_directive in route_directives:
        path, sections = route_directive
        in_section = False
        last_intersection = None

        for intersection in p_cache[path]:
            if len(sections) == 0:
                break

            secondary_street = sections[0][0] if not in_section else sections[0][1]
            int_name1 = ' and '.join([path, secondary_street])
            int_name2 = ' and '.join([secondary_street, path])
            # Start the section if needed. We will add route directive on the NEXT intersection.
            if not in_section and (int_name1 == intersection or int_name2 == intersection):
                in_section = True

            # Add the route directive
            elif in_section:
                key_name = '%s | %s' % (last_intersection, intersection)
                rd_cache[key_name] = sections[0][2]

                # Are we done with this section?
                if int_name1 == intersection or int_name2 == intersection:
                    in_section = False
                    sections.pop(0)

                    # Start the NEXT section immediately if needed.
                    if len(sections) == 0:
                        break
                    secondary_street = sections[0][0] if not in_section else sections[0][1]
                    int_name1 = ' and '.join([path, secondary_street])
                    int_name2 = ' and '.join([secondary_street, path])

                    if int_name1 == intersection or int_name2 == intersection:
                        in_section = True


            last_intersection = intersection

    # Get any route directives in custom paths as well
    # Note; we need to use the p_cache, since it has been sorted and cleaned (i.e. Baker St + Fell St -> Fell St + Baker St)
    custom_paths = get_custom_paths(input_data)
    for custom_path, entry in custom_paths.iteritems():
        if 'type' in entry and entry['type'] in ['route', 'path']:
            for index, intersection in enumerate(p_cache[custom_path]):
                if index+1 == len(p_cache[custom_path]):
                    continue
                key_name = '%s | %s' % (intersection, p_cache[custom_path][index+1])
                rd_cache[key_name] = entry['type']

    # BREAKS OVERRIDE ROUTE DIRECTIVES
    cache['route_directives'] = rd_cache
    return cache

#################
# main script executable
#################

parser = argparse.ArgumentParser()

parser.add_argument('-f', '--force', action='store_true', help='force an overwrite of any existing scraped keys')
parser.add_argument('-v', '--verbose', action='store_true', help='display all informational logging')
parser.add_argument('-d', '--debug', action='store_true', help='display all debug logging')
parser.add_argument('input_data', help="input data file (i.e. data/sf_test.py)")
parser.add_argument('output_file', help="output file location")
parser.add_argument('bad_cache', help="cache for bad addresses")


if __name__ == "__main__":
    now = time.time()
    args = parser.parse_args()
    print args


    if args.verbose:
        logging.getLogger().setLevel(logging.INFO)
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    logging.getLogger('requests.packages.urllib3.connectionpool').setLevel(logging.WARNING)

    # Set the cache from an existing output file
    if args.force or not os.path.exists(args.output_file):
        cache = {'paths': {}, 'intersections': {}, 'directions': {}, 'custom_path_names': []}
    else:
        with open(args.output_file) as filecache:
            cache = json.load(filecache)
            for key in ['paths', 'intersections', 'directions']:
                if key not in cache:
                    cache[key] = {}
            cache['custom_path_names'] = []

    # Get the bad address cache, if it exists
    if args.bad_cache and os.path.exists(args.bad_cache):
        with open(args.bad_cache) as bcache_fp:
            bad_address_cache = load_bad_address_cache(bcache_fp)
    else:
        bad_address_cache = create_empty_bad_address_cache()


    # Get the intersection data
    intersections = compute_all_intersections(args.input_data)

    city = get_city(args.input_data)

    # Lookup every intersection's lat/lng/elevation, fill out the paths json
    cache, bad_address_cache, stats = lookup_all_intersections(cache, intersections, bad_address_cache, city)

    # Look up custom paths. These should all be ordered, so we do not need to sort these paths!
    cache = lookup_and_add_custom_paths(cache, args.input_data, city)

    # Sort the paths json. TODO: fix docs - this also adds BREAKs into the paths.
    cache = sort_path_cache(cache, args.input_data)

    # Get any custom Google Directions API info we need.
    cache = lookup_curved_road_directions(cache, args.input_data, city)

    # Get the route directive definitions (bike paths, etc)
    cache = define_route_directives(cache, args.input_data)

    cache['tbds'] = {}
    for tbd, latlng in get_tbds(args.input_data).iteritems():
        cache['tbds'][tbd] = {'lat': latlng[0], 'lng': latlng[1]}


    cache['buildtimestamp'] = int(now)
    cache['buildtimereadable'] = datetime.datetime.fromtimestamp(now).strftime('%Y-%m-%d-%H:%M')

    with open(args.output_file, 'w') as result_file:
        json.dump(cache, result_file, indent=2, separators=(',', ': '), sort_keys=True)

    # Minified
    with open('-min.'.join(args.output_file.split('.', 1)), 'w') as min_result_file:
        json.dump(cache, min_result_file, sort_keys=True)

    with open(args.bad_cache, 'w') as bcache_fp:
        write_bad_address_cache(bcache_fp, bad_address_cache)

    print "total intersections:", len(intersections)
    print "good intersections looked up:", stats['good']
    print "bad intersections looked up and to be skipped next time:", stats['bad']
    print "cached intersections:", stats['cached']
    print "bad skipped intersections:", stats['skipped']
    print "error on lookup:", stats['error']

    logging.info('Done!')
