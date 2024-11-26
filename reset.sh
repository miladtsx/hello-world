docker rm -f $(docker ps -aq)
rm hello_world* -rf
autonomy packages lock
autonomy fetch valory/hello_world:0.1.0 --local
cd hello_world
echo -n "0x47e179ec197488593b187f80a00eb0da91f1b9d0b13f8733639f19c30a34926a" > your_agent_key.txt
autonomy add-key ethereum your_agent_key.txt
cd ..
autonomy analyse service --public-id  valory/hello_world:0.1.0
autonomy push-all
autonomy fetch valory/hello_world:0.1.0 --service --local --alias hello_world_service
cd hello_world_service
autonomy build-image
autonomy generate-key ethereum -n 4
export ALL_PARTICIPANTS=$(jq -c '[.[].address]' keys.json)
autonomy deploy build ./keys.json -ltm
autonomy deploy run --build-dir ./abci_build/