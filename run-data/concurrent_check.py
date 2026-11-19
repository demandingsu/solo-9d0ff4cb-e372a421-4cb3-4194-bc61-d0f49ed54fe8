"""真实 HTTP 下的并发确认与交错更正走查。"""
import concurrent.futures
import http.client
import json
import threading

HOST, PORT = "127.0.0.1", 8000

def req(method, path, body=None, headers=None):
    c = http.client.HTTPConnection(HOST, PORT, timeout=30)
    c.request(method, path, body=body, headers=headers or {})
    r = c.getresponse()
    data = r.read()
    c.close()
    return r.status, data

def multipart(path, rows_text, filename="x.csv"):
    boundary = "----ctdboundary"
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
            f"filename=\"{filename}\"\r\nContent-Type: text/csv\r\n\r\n{rows_text}"
            f"\r\n--{boundary}--\r\n").encode()
    return req("POST", path, body, {"Content-Type": f"multipart/form-data; boundary={boundary}"})

header = "投放号,采样序号,深度,水温,盐度\n"
def csv_of(depths):
    return header + "".join(f"CAST-C,{i},{d},20,34\n" for i, d in enumerate(depths, 1))

# 准备 v1
s, b = multipart("/api/uploads", csv_of([1.0, 2.0]))
v1 = json.loads(b)["version_id"]
print("v1 上传:", s, "version_id=", v1)

# 场景 A：并发确认同一草稿版
barrier = threading.Barrier(4)
def publish_same():
    barrier.wait()
    return req("POST", f"/api/versions/{v1}/publish")[0]
with concurrent.futures.ThreadPoolExecutor(4) as ex:
    codes = list(ex.map(lambda _: publish_same(), range(4)))
print("并发确认同一版状态码:", sorted(codes), "(期望全 200，幂等)")
s, b = req("GET", "/api/casts/CAST-C/versions")
vers = json.loads(b)
assert len(vers) == 1 and vers[0]["status"] == "published"

# 场景 B：交错——确认 v2 草稿与 v3 更正同时进行
s, b = multipart("/api/uploads", csv_of([1.0, 2.0, 3.0]))
v2 = json.loads(b)["version_id"]
print("v2 草稿:", s, "version_id=", v2)
barrier = threading.Barrier(2)
def pub_v2():
    barrier.wait()
    return req("POST", f"/api/versions/{v2}/publish")
def up_v3():
    barrier.wait()
    return multipart("/api/uploads", csv_of([1.0, 2.0, 3.0, 4.0]))
with concurrent.futures.ThreadPoolExecutor(2) as ex:
    f1 = ex.submit(pub_v2); f2 = ex.submit(up_v3)
    (ps, pb), (us, ub) = f1.result(), f2.result()
print(f"交错结果: 确认v2={ps}, 上传v3={us}")
s, b = req("GET", "/api/casts/CAST-C/versions")
vers = json.loads(b)
print("版本状态:", [(f"v{v['version_no']}", v["status"]) for v in vers])
s, b = req("GET", "/api/casts/CAST-C/profile.csv")
print("交错后现行下载:", s, "(409=待确认；200=v2曾发布但已被取代则应为409)")
# 无论哪条路径，最终确认最新版 v3 后必须恢复，且内容只来自 v3
v3 = vers[-1]["id"]
ps, _ = req("POST", f"/api/versions/{v3}/publish")
print("确认 v3:", ps)
s, b = req("GET", "/api/casts/CAST-C/profile.csv")
text = b.decode()
print("恢复下载:", s, "| 含v3:", "v3" in text, "| 数据区含v2:", "v2" in text.split("META",1)[1])
assert s == 200 and "v3" in text and "v2" not in text.split("META",1)[1]
print("\n全部并发断言通过")
