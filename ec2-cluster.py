import os
import time
import boto
import boto.ec2
from tabulate import tabulate
from jinja2 import Environment, FileSystemLoader
import sys, argparse
import yaml


def launch_instance(ec2, ami, instance_type, region, groups, key_name ):

    print 'Launch instance [ami=%s, type=%s, groups=%s, key=%s'%(ami, instance_type, groups, key_name)

    reservation = ec2.run_instances(ami, key_name=key_name, security_groups=groups, instance_type=instance_type, placement=region )

    instance = reservation.instances[0]

    # Wait for instance state to change to 'running'.
    print 'waiting for instance'
    while instance.state != 'running':
        print '.'
        time.sleep(5)
        instance.update()
    print 'done'

    return instance

def tag_resource(ec2, resource, tags):
    print 'Tagging %s with %s.'%(resource, tags)
    ec2.create_tags(resource, tags)

def create_security_rule(ec2,vpc,group,protocol,start,end,cidr):
    # Check to see if specified security group already exists.
    # If we get an InvalidGroup.NotFound error back from EC2,
    # it means that it doesn't exist and we need to create it.

    print 'Creating security rule %s:%s[%s,%s]->%s'%(group,protocol,start,end,cidr)
    try:
        ec2_group = ec2.get_all_security_groups(groupnames=[group])[0]
    except ec2.ResponseError, e:
        if e.code == 'InvalidGroup.NotFound':
            print 'Creating Security Group: %s' % group
            # Create a security group to control access to instance via SSH.
            ec2_group = ec2.create_security_group(group, group, vpc_id=vpc)
        else:
            raise

    try:
        ec2_group.authorize(protocol, start, end, cidr)
    except ec2.ResponseError, e:
        if e.code == 'InvalidPermission.Duplicate':
            print 'Security Group: %s already authorized' % group
        else:
            raise

def create_key_pair(ec2, key_name, key_dir, key_extension='.pem'):
    # Create an SSH key to use when logging into instances.
    # Check to see if specified keypair already exists.
    # If we get an InvalidKeyPair.NotFound error back from EC2,
    # it means that it doesn't exist and we need to create it.

    print 'Create key_pair %s at %s'%(key_name, key_dir)
    try:
        key = ec2.get_all_key_pairs(keynames=[key_name])[0]
        print 'Keypair found. Not creating.'
    except ec2.ResponseError, e:
        if e.code == 'InvalidKeyPair.NotFound':
            print 'Creating keypair: %s' % key_name

            key = ec2.create_key_pair(key_name)
            # AWS will store the public key but the private key is
            # generated and returned and needs to be stored locally.
            # The save method will also chmod the file to protect
            # your private key.
            key.save(key_dir)
        else:
            raise

def create_volume(ec2, region, volume_size):

    print 'Create and attach %s EBS volume to %s at %s' % (volume_size,region, device_name)
    # Determine the Availability Zone of the instance

    volume = ec2.create_volume(volume_size, region)
    # Wait for the volume to be created.
    while volume.status != 'available':
        time.sleep(5)
        volume.update()
    return volume

def attach_volume(instance, volume, device_name):
    volume.attach(instance.id, device_name)
    return volume

def connect(aws):
    # Create a connection to EC2 service.
    # You can pass credentials in to the connect_ec2 method explicitly
    # or you can use the default credentials in your ~/.boto config file
    # as we are doing here.
    conn = boto.ec2.connect_to_region(aws['region'],aws_access_key_id=aws['access_key'], aws_secret_access_key=aws['secret_key'])
    print conn
    return conn

def get_all_instances(ec2, tags):
    instances = []
    print tags
    tags = {'tag:'+k:v for k,v in tags.iteritems()}
    print tags
    reservations = ec2.get_all_instances(filters=tags)
    for r in reservations:
        print r
        for instance in r.instances:
            if instance.state == 'running':
                instances.append(instance)

    return instances

def get_all_volumes(ec2, tags):
    tags = {'tag:'+k:v for k,v in tags.iteritems()}
    tags
    return ec2.get_all_volumes(filters=tags)

def list(aws, inventory, cluster=None, facet=None, index=None):
    ec2 = connect(aws)
    tags = {'env' : inventory.get_env()}
    if cluster:
        tags['cluster'] = cluster
    if facet:
        tags['facet'] = facet
    if index:
        tags['index'] = index

    print(tags)
    volumes = get_all_volumes(ec2,tags)
    print volumes
    instances = get_all_instances(ec2, tags)

    i_data = get_data_from_instances(instances)
    keys = i_data[0].keys()
    t = [[v for k,v in i.iteritems()] for i in i_data]

    print tabulate(t, headers=keys,tablefmt="grid")


def launch(aws, inventory, credentials_dir, cluster=None, facet=None, index=None):

    # 2. Create keypair for env if it does not exist.
    ec2 = connect(aws)
    create_key_pair(ec2, inventory.get_env(), credentials_dir)

    # 3. Create security groups if they do not exist.
    # 4. Apply rules.
    rules = inventory.get_security_rules()
    for rule in rules:
        create_security_rule(ec2, rule['vpc'],rule['group'], rule['protocol'], rule['start'], rule['end'], rule['cidr'] )

    # 1. Get inventory of machines

    instances = inventory.get_instances(cluster, facet, index)
    print instances
    for i in instances:
        # 5. Launch instance if it does not exist.
        existing_instances = get_all_instances(ec2, {'Name': i['name']})
        if(len(existing_instances) > 0):
            print 'Instance %s exists' % i['name']
        else:
            instance = launch_instance(ec2, i['image'], i['type'], i['region'], i['security'], inventory.get_env() )
            # 6. Tag the instance. (Name, env, cluster, facet, index)
            tags = {'Name': i['name'], 'env': i['env'], 'cluster': i['cluster'], 'facet': i['facet'], 'index': i['index']}
            tag_resource(ec2, instance.id, tags)

            # TODO: Tag root volume.
            # 7. Create EBS volume and attach it to the instance.
            # 8. Assign the same name and tags to it.
            if(i['volume'] != None):
                vols = get_all_volumes(ec2, tags)
                if(len(vols) > 0):
                    print 'Volume already exists for tags : %s.'% tags
                    vol = vols[0]
                else:
                    vol = create_volume(ec2, i['region'],i['volume'])
                    tag_resource(ec2, vol.id, tags)

                attach_volume(instance, vol, '/dev/sdh')

            # 9. TODO: Update the route53 record.
            # 10. Update the inventory file.
            all_instances = get_all_instances(ec2, {'env': i['env']} )
            inventory.update_inventory_file(get_data_from_instances(all_instances))

def get_data_from_instances(instances):
    instance_data = []
    for instance in instances:
        data = {}
        data['Id'] = instance.id
        data['Name'] = instance.tags['Name']
        data['Env'] = instance.tags['env']
        data['Cluster'] = instance.tags['cluster']
        data['Facet'] = instance.tags['facet']
        data['Index'] = instance.tags['index']
        data['Public DNS'] = instance.public_dns_name
        data['AMI'] = instance.image_id
        data['Launch Time'] = instance.launch_time
        data['VPC'] = instance.vpc_id
        data['Public IP'] = instance.ip_address
        data['Private IP'] = instance.private_ip_address
        data['State'] = instance.state
        instance_data.append(data)

    return instance_data

def update(e2, cluster=None, facet=None, index=None):
    # Update SG and rules
    # Update assignment of instance to SGs.
    # Update tags
    pass


class Inventory(object):
    def __init__(self, config_file, inventory_file='hosts'):
        self.raw = yaml.load(open(config_file))
        self._denormalize_()
        self.inventory_file = inventory_file

    def get_env(self):
        return self.raw['env']

    def get_instances(self, cluster='.', facet='.', index='.'):

        # TODO : Validate if the facet/cluster/index exists
        instances = self.instances
        if cluster:
            instances = [k for k in instances if k['cluster'] == cluster]
        if facet:
            instances = [k for k in instances if k['facet'] == facet]
        if index:
            instances = [k for k in instances if k['index'] == index]

        return instances


    def get_security_rules(self, name=None):
        rules = self.rules
        if name:
            rules = [i for i in self.rules if i['group'] == name]
        return rules

    def update_inventory_file(self, instances):
        clusters = [i for i in self.raw['clusters']]

        facets = {i:[f for f in self.raw['clusters'][i]['facets']] \
                    for i in self.raw['clusters']}

        instances= {c:{f:[i for i in instances if i['Facet'] == f] \
                       for f in self.raw['clusters'][c]['facets']} \
                           for c in self.raw['clusters']}


        env = Environment(loader=FileSystemLoader(os.path.dirname(__file__)))
        template = env.get_template('hosts.j2')
        s = template.render({'env': self.raw['env'], 'clusters' : clusters, 'facets' : facets, 'instances' : instances})
        print s
        with open(self.inventory_file, "wb") as fh:
            fh.write(s)

    def _denormalize_(self):
        self.instances = []

        for cluster in self.raw['clusters']:
            facets = self.raw['clusters'][cluster]['facets']
            for facet in facets:
                count = self.raw['clusters'][cluster]['facets'][facet]['count']
                for index in xrange(count):
                    name = "%s-%s-%s-%s"%(self.raw['env'],cluster,facet,index)

                    details = {}
                    details['name'] = name

                    # Add env/cluster level data
                    details['env']= self.raw['env']
                    details['cluster'] = cluster
                    details['facet']= facet
                    details['index']=index
                    details['image']= self.raw['image']
                    details['vpc']=self.raw['vpc']
                    details['region']=self.raw['region']

                    # Add facet data
                    details.update(self.raw['clusters'][cluster]['facets'][facet])

                    # Add security data
                    if not details.has_key('security'):
                        details['security'] = []
                    details['security'].append('default')
                    details['security'] = ["%s-%s"%(self.raw['env'],i) for i in details['security']]
                    self.instances.append( details)


        self.rules = []
        for group in self.raw['security']:
            for protocol in self.raw['security'][group]:
                for rule in self.raw['security'][group][protocol]:
                    details = {}
                    details['vpc'] = self.raw['vpc']
                    details['group'] = "%s-%s"%(self.raw['env'], group)
                    details['protocol'] = protocol
                    details[ 'cidr'] = rule
                    details[ 'start'] = self.raw['security'][group][protocol][rule][0]
                    details['end'] = self.raw['security'][group][protocol][rule][1]

                    self.rules.append(details)



def main(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument("-c", help="Path to aws credentials.(Default=credentials/aws.yaml", action="store_true")
    parser.add_argument("action", help="launch|show|kill|update (Only launch is implemented.)")
    parser.add_argument("env",  help="Environment")
    parser.add_argument("cluster",  help="Cluster")
    parser.add_argument("facet", help="Facet")
    parser.add_argument("index", type=int, help="Index")
    args = parser.parse_args()

    print args.action
    print args.cluster
    print args.facet
    print args.index

    inventory = Inventory('%s.yaml'%(args.env))
    aws = yaml.load(open('credentials/aws.yaml'))
    if(args.action == 'launch'):
        launch(aws, inventory,'credentials/ec2_keys',args.cluster,args.facet, args.index )
    elif(args.action == 'list'):
        list(aws, inventory)
    elif(args.action == 'ex'):
        ec2 = connect(aws)
        all_instances = get_all_instances(ec2, {'env': 'sandbox'} )
        inventory.update_inventory_file(get_data_from_instances(all_instances))

if __name__ == "__main__":
   main(sys.argv[1:])
