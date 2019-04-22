from asyncio import gather
from collections import defaultdict

from sanic import Sanic
from sanic.response import json

from blueprints.index import index_bp
from connections import get_client

app = Sanic()
app.blueprint(index_bp, url_prefix='/api/v1/index')


def format_index_data(data):
    if data['status'] == 'close':
        return {
            "status": data["status"],
        }
    return {
        "primaries": int(data["pri"]),
        "replicas": int(data["rep"]),
        "status": data["status"],
        "docsCount": int(data["docs.count"]),
        "docsDeleted": int(data["docs.deleted"]),
        "storeSize": data["store.size"],
    }


async def get_cluster_info():
    client = get_client()
    [info], [docs] = await gather(client.cat.health(format='json'),
                                  client.cat.count(format='json'))
    return {
        "relocatingShards": int(info['relo']),
        "initializingShards": int(info['init']),
        "unassignedShards": int(info['unassign']),
        "numOfPrimaryShards": int(info['pri']),
        "numOfReplicaShards": int(info["shards"]) - int(info['pri']),
        "numberOfNodes": int(info['node.total']),
        "numberOfDocs": int(docs['count']),
        "clusterName": info['cluster'],
        "clusterStatus": info['status'].title(),
    }


@app.route('/api/v1/shards_grid')
async def indices_stats(request):
    client = get_client()
    indices, shards, nodes, cluster_info = await gather(client.cat.indices(format='json'),
                                                        client.cat.shards(format='json'),
                                                        client.cat.nodes(format='json'),
                                                        get_cluster_info())
    cluster_info["numOfIndices"] = len(indices)
    indices_per_node = defaultdict(lambda: defaultdict(lambda: {'replicas': [], 'primaries': []}))
    relocating_indices = list({shard['index'] for shard in shards if shard['state'] == 'RELOCATING'})
    recovery = await client.cat.recovery(index=relocating_indices, format='json')
    relocation_progress = {
        (recovery_data["index"], recovery_data["shard"]):
            int(float(recovery_data["bytes_recovered"]) * 100 / int(recovery_data["bytes_total"]))
        for recovery_data in recovery if recovery_data["stage"] != "done"
    }
    relocating = 0
    for shard in shards:
        if shard['prirep'] == 'p':
            shard_type = 'primaries'
        elif shard['prirep'] == 'r':
            shard_type = 'replicas'
        else:
            raise RuntimeError('Unknown shard type %s' % shard['prirep'])
        data = {
            "shard": int(shard['shard']),
            "state": shard['state'],
        }
        node = shard['node']
        if shard['state'] == 'RELOCATING':
            relocating += 1
            node = node.split(' ->')[0]
            data['progress'] = relocation_progress[(shard['index'], shard['shard'])]
        indices_per_node[node][shard['index']][shard_type].append(data)
        indices_per_node[node][shard['index']][shard_type].sort(key=lambda x: x['shard'])

    nodes_result = []
    for node in nodes:
        nodes_result.append({
            "name": node["name"],
            "indices": dict(sorted(indices_per_node[node["name"]].items())),
            "isMaster": node["master"] == "*",
            "ip": node["ip"],
            "role": node["node.role"],
            "metrics": {
                # Reduce precision in order to reduce render loops in UI
                "CPUPercent": int(float(node["cpu"])),
                "heapPercent": int(float(node["heap.percent"])),
                "load1Percent": int(float(node["load_1m"]) / 4)  # TODO: Count CPUs
            }
        })

    return json({
        "nodes": sorted(nodes_result, key=lambda x: x["name"]),
        "indices": dict([(x['index'], format_index_data(x)) for x in indices]),
        "cluster": cluster_info,
    })


@app.route('/api/v1/cluster/reroute_shards', methods=['POST'])
async def reroute_shard(request):
    client = get_client()
    node = request.json['node']
    shards = request.json['shards']
    await client.cluster.reroute(
        body={"commands": [
            {"move": {"index": shard['index'], "shard": shard['id'], "from_node": shard['nodeName'], "to_node": node}}
            for shard in shards
        ]}
    )
    return json({"status": "ok"})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8000, debug=True)