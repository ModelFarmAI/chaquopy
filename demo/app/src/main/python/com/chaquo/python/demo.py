from java import *

from android.app import AlertDialog
from android.content import Context, DialogInterface
from android.os import Bundle
from android.support.v4.app import DialogFragment
from android.support.v7.app import AppCompatActivity
from android.support.v7.preference import Preference, PreferenceFragmentCompat
from com.chaquo.python.demo import R
from java.lang import String


class UIDemoActivity(static_proxy(AppCompatActivity)):
    @Override(jvoid, [Bundle])
    def onCreate(self, savedInstanceState):
        super(UIDemoActivity, self).onCreate(savedInstanceState)
        self.setContentView(R.layout.activity_menu)
        t = self.getSupportFragmentManager().beginTransaction()
        t.replace(R.id.flMenu, MenuFragment()).commit()


class MenuFragment(static_proxy(PreferenceFragmentCompat)):
    @Override(jvoid, [Bundle, String])
    def onCreatePreferences(self, savedInstanceState, rootKey):
        self.addPreferencesFromResource(R.xml.activity_ui_demo)

    @Override(jboolean, [Preference])
    def onPreferenceTreeClick(self, pref):
        dispatch = {self.getContext().getString(getattr(R.string, key)): getattr(self, key)
                    for key in ["demo_dialog", "demo_notify", "demo_toast", "demo_sound",
                                "demo_vibrate"]}
        method = dispatch.get(str(pref.getTitle()))
        if method:
            method(self.getContext())
            return True
        else:
            return False

    def demo_dialog(self, context):
        ColorDialog().show(self.getFragmentManager(), "color")

    def demo_notify(self, context):
        from android.app import Notification
        builder = Notification.Builder(context)
        builder.setSmallIcon(R.drawable.ic_launcher)
        builder.setContentTitle(context.getString(R.string.demo_notify_title))
        builder.setContentText(context.getString(R.string.demo_notify_text))
        context.getSystemService(Context.NOTIFICATION_SERVICE).notify(0, builder.build())

    def demo_toast(self, context):
        from android.widget import Toast
        Toast.makeText(context, R.string.demo_toast_text, Toast.LENGTH_SHORT).show()

    def demo_sound(self, context):
        from android.media import MediaPlayer
        from android.provider import Settings
        MediaPlayer.create(context, Settings.System.DEFAULT_NOTIFICATION_URI).start()

    def demo_vibrate(self, context):
        context.getSystemService(Context.VIBRATOR_SERVICE).vibrate(200)


class ColorDialog(static_proxy(DialogFragment)):
    @Override(AlertDialog, [Bundle])
    def onCreateDialog(self, savedInstanceState):
        activity = self.getActivity()
        builder = AlertDialog.Builder(activity)
        builder.setTitle(R.string.demo_dialog_title)
        builder.setMessage(R.string.demo_dialog_text)

        class Listener(dynamic_proxy(DialogInterface.OnClickListener)):
            def __init__(self, resId):
                super(Listener, self).__init__()
                self.color = activity.getResources().getColor(resId)

            def onClick(self, dialog, which):
                from android.graphics.drawable import ColorDrawable
                activity.getSupportActionBar().setBackgroundDrawable(ColorDrawable(self.color))

        builder.setNegativeButton(R.string.red, Listener(R.color.red))
        builder.setNeutralButton(R.string.green, Listener(R.color.green))
        builder.setPositiveButton(R.string.blue, Listener(R.color.blue))
        return builder.create()
