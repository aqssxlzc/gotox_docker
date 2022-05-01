gotox_docker
========
A docker warpper for gotox 

运行
========
```bash

docker build  -t gotox .
docker run -d --publish 8087:8087 gotox 


代理
=======
设置8087 为代理服务器，ex等网站应该可以访问了