package dev.bubuki.proactive;
import android.app.*;
import android.content.*;
import org.json.*;

public class AgentActionReceiver extends BroadcastReceiver {
    static PendingIntent pending(Context c,String action,String id){
        return pending(c,action,id,id==null?0:id.hashCode());
    }
    static PendingIntent pending(Context c,String action,String id,int notifId){
        Intent i=new Intent(c,AgentActionReceiver.class).setAction(action).putExtra("id",id).putExtra("notifId",notifId);
        return PendingIntent.getBroadcast(c,(action+":"+id+":"+notifId).hashCode(),i,PendingIntent.FLAG_IMMUTABLE|PendingIntent.FLAG_UPDATE_CURRENT);
    }
    @Override public void onReceive(Context c,Intent i){
        String action=i.getAction(),id=i.getStringExtra("id");
        AgentForegroundService.stopEmergencyAlarm(c);
        if("stop_alarm".equals(action)){
            return;
        }
        int notifId=i.getIntExtra("notifId",id==null?0:id.hashCode());
        c.getSystemService(NotificationManager.class).cancel(notifId);
        PendingResult pending=goAsync();
        new Thread(()->{try{
            JSONObject body=new JSONObject();
            if("ack".equals(action))body.put("ids",new JSONArray().put(id));
            else body.put("minutes",i.getIntExtra("minutes",60));
            new SecureTransport(c).request("POST","ack".equals(action)?"/api/ack":"/api/snooze",body,null);
            c.sendBroadcast(new Intent(c.getPackageName()+".UPDATE").setPackage(c.getPackageName()));
        }catch(Exception ignored){}finally{pending.finish();}},"pulse-action").start();
    }
}
